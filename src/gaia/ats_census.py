from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .career_surface_collector import CareerSurfaceCollector
from .collectors import AshbyCollector, Collector, GreenhouseCollector, LeverCollector
from .live_inventory import LiveDatabase
from .market_collectors import WorkdaySearchCollector
from .provider_collectors import (
    ICIMSCollector,
    JobviteCollector,
    RecruiteeCollector,
    SmartRecruitersCollector,
    WorkableCollector,
)
from .quality import canonical_source_name
from .source_catalog import _collector, _spec, save_candidates

CENSUS_VERSION = 1
COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
RETRYABLE = {429, 500, 502, 503, 504}
PATH_DENY = frozenset({"admin", "api", "app", "assets", "auth", "blog", "cdn", "docs", "embed", "help", "login", "static", "status", "support", "www"})
SUBDOMAIN_DENY = frozenset({"admin", "api", "app", "assets", "auth", "cdn", "login", "static", "www"})
SLUG = r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?"


@dataclass(frozen=True, slots=True)
class Pattern:
    provider: str
    queries: tuple[str, ...]
    regex: re.Pattern[str]
    mode: str
    seed: str | None = None


PATTERNS = (
    Pattern("greenhouse", ("boards.greenhouse.io/*", "job-boards.greenhouse.io/*"), re.compile(rf"(?:boards|job-boards)\.greenhouse\.io/({SLUG})(?:[/?#]|$)", re.I), "greenhouse"),
    Pattern("lever", ("jobs.lever.co/*",), re.compile(rf"jobs\.lever\.co/({SLUG})(?:[/?#]|$)", re.I), "lever"),
    Pattern("ashby", ("jobs.ashbyhq.com/*",), re.compile(rf"jobs\.ashbyhq\.com/({SLUG})(?:[/?#]|$)", re.I), "ashby"),
    Pattern("smartrecruiters", ("jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*"), re.compile(rf"(?:jobs|careers)\.smartrecruiters\.com/({SLUG})(?:[/?#]|$)", re.I), "smartrecruiters"),
    Pattern("recruitee", ("*.recruitee.com/*",), re.compile(rf"https?://({SLUG})\.recruitee\.com(?:[/:?#]|$)", re.I), "recruitee"),
    Pattern("workable", ("*.workable.com/*",), re.compile(rf"https?://(?:apply\.workable\.com/({SLUG})|({SLUG})\.workable\.com)(?:[/?#]|$)", re.I), "workable"),
    Pattern("jobvite", ("jobs.jobvite.com/*",), re.compile(rf"jobs\.jobvite\.com/({SLUG})(?:[/?#]|$)", re.I), "jobvite"),
    Pattern("icims", ("*.icims.com/*",), re.compile(rf"https?://({SLUG})\.icims\.com(?:[/:?#]|$)", re.I), "icims"),
    Pattern("workday", ("*.myworkdayjobs.com/*",), re.compile(rf"https?://(({SLUG})\.wd\d+(?:-[a-z0-9-]+)?\.myworkdayjobs\.com)/wday/cxs/([^/]+)/([^/?#]+)", re.I), "workday"),
    Pattern("teamtailor", ("*.teamtailor.com/*",), re.compile(rf"https?://({SLUG})\.teamtailor\.com(?:[/:?#]|$)", re.I), "domain", "https://{slug}.teamtailor.com/jobs"),
    Pattern("bamboohr", ("*.bamboohr.com/*",), re.compile(rf"https?://({SLUG})\.bamboohr\.com(?:[/:?#]|$)", re.I), "domain", "https://{slug}.bamboohr.com/careers"),
    Pattern("breezy", ("*.breezy.hr/*",), re.compile(rf"https?://({SLUG})\.breezy\.hr(?:[/:?#]|$)", re.I), "domain", "https://{slug}.breezy.hr"),
    Pattern("personio", ("*.jobs.personio.com/*",), re.compile(rf"https?://({SLUG})\.jobs\.personio\.com(?:[/:?#]|$)", re.I), "domain", "https://{slug}.jobs.personio.com"),
)


def _humanize(slug: str) -> str:
    return " ".join(part.capitalize() for part in re.sub(r"[-_]+", " ", slug).split()) or slug


def _slug(match: re.Match[str], pattern: Pattern) -> str | None:
    if pattern.mode == "workable":
        value = (match.group(1) or match.group(2) or "").casefold()
        deny = PATH_DENY | SUBDOMAIN_DENY
    else:
        value = (match.group(1) or "").casefold()
        deny = SUBDOMAIN_DENY if pattern.mode in {"recruitee", "icims", "domain"} else PATH_DENY
    return value if value and value not in deny else None


def _collector_from_match(pattern: Pattern, match: re.Match[str]) -> Collector | None:
    if pattern.mode == "workday":
        host, tenant, site = match.group(1).casefold(), match.group(3), match.group(4)
        if not tenant or not site:
            return None
        return WorkdaySearchCollector(_humanize(tenant), f"https://{host}", tenant, site)
    slug = _slug(match, pattern)
    if not slug:
        return None
    company = _humanize(slug)
    if pattern.mode == "greenhouse":
        return GreenhouseCollector(company, slug)
    if pattern.mode == "lever":
        return LeverCollector(company, slug)
    if pattern.mode == "ashby":
        return AshbyCollector(company, slug)
    if pattern.mode == "smartrecruiters":
        return SmartRecruitersCollector(company, slug)
    if pattern.mode == "recruitee":
        return RecruiteeCollector(company, slug)
    if pattern.mode == "workable":
        return WorkableCollector(company, slug)
    if pattern.mode == "jobvite":
        return JobviteCollector(company, slug)
    if pattern.mode == "icims":
        return ICIMSCollector(company, f"{slug}.icims.com")
    if pattern.mode == "domain" and pattern.seed:
        seed = pattern.seed.format(slug=slug)
        collector = CareerSurfaceCollector(company, urlsplit(seed).netloc, [seed])
        collector.name = f"domain:{pattern.provider}:{slug}"
        return collector
    return None


def parse_cdx(body: str) -> list[str]:
    urls = []
    for line in body.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        if str(row.get("status") or "200") == "200":
            urls.append(str(row["url"]))
    return urls


def parse_pages(body: str) -> int:
    try:
        return max(0, int(body.strip()))
    except ValueError:
        pass
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 0
    try:
        return max(0, int(payload.get("pages") or 0)) if isinstance(payload, dict) else 0
    except (TypeError, ValueError):
        return 0


def extract_collectors(urls: list[str], pattern: Pattern) -> list[Collector]:
    found: dict[str, Collector] = {}
    for url in urls:
        for match in pattern.regex.finditer(url):
            collector = _collector_from_match(pattern, match)
            if collector is None:
                continue
            source = canonical_source_name(collector.name)
            if source:
                collector.name = source
                found[source] = collector
    return list(found.values())


def serialize_collectors(collectors: list[Collector]) -> list[dict[str, Any]]:
    rows = []
    for collector in collectors:
        described = _spec(collector)
        source = canonical_source_name(collector.name)
        if described is None or not source:
            continue
        kind, spec = described
        rows.append({"source": source, "kind": kind, "scope": collector.scope, "spec": spec})
    return rows


def deserialize_collectors(rows: list[dict[str, Any]]) -> list[Collector]:
    found: dict[str, Collector] = {}
    for row in rows:
        source = canonical_source_name(str(row.get("source") or ""))
        spec = row.get("spec")
        if not source or not isinstance(spec, dict):
            continue
        try:
            collector = _collector(str(row.get("kind") or ""), spec)
        except (KeyError, TypeError, ValueError):
            continue
        if collector is None:
            continue
        collector.name = source
        collector.scope = str(row.get("scope") or "current")
        found[source] = collector
    return list(found.values())


class CommonCrawl:
    def __init__(self, client: httpx.AsyncClient, delay: float) -> None:
        self.client = client
        self.delay = max(0.25, delay)

    async def get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        attempts = max(1, int(os.getenv("GAIA_CENSUS_HTTP_RETRIES", "4")))
        response = None
        for attempt in range(attempts):
            response = await self.client.get(url, params=params)
            if response.status_code not in RETRYABLE:
                response.raise_for_status()
                await asyncio.sleep(self.delay)
                return response
            if attempt + 1 < attempts:
                raw = response.headers.get("Retry-After")
                try:
                    wait = float(raw) if raw else 2.0 ** (attempt + 1)
                except ValueError:
                    wait = 2.0 ** (attempt + 1)
                await asyncio.sleep(min(60.0, max(self.delay, wait)))
        assert response is not None
        response.raise_for_status()
        return response

    async def collections(self, count: int) -> list[str]:
        payload = (await self.get(COLLINFO_URL)).json()
        if not isinstance(payload, list):
            raise ValueError("invalid Common Crawl collection list")
        ids = [str(item.get("id") or "") for item in payload if isinstance(item, dict)]
        return [item for item in ids if item.startswith("CC-MAIN-")][: max(1, count)]

    async def urls(self, collection: str, query: str, max_pages: int) -> list[str]:
        endpoint = f"https://index.commoncrawl.org/{collection}-index"
        page_info = await self.get(endpoint, {"url": query, "showNumPages": "true"})
        pages = max(1, min(max_pages, parse_pages(page_info.text) or 1))
        urls = []
        for page in range(pages):
            response = await self.get(
                endpoint,
                {"url": query, "output": "json", "fl": "url,status,timestamp", "filter": "status:200", "page": str(page)},
            )
            urls.extend(parse_cdx(response.text))
        return urls


async def build_snapshot(snapshot_count: int, max_pages: int, delay: float) -> dict[str, Any]:
    headers = {"User-Agent": os.getenv("GAIA_USER_AGENT", "GAIA/5.0 ATS-census (+https://github.com/catears124/GAIA)")}
    timeout = httpx.Timeout(float(os.getenv("GAIA_CENSUS_HTTP_TIMEOUT", "45")))
    found: dict[str, Collector] = {}
    provider_counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    completed = 0
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        cc = CommonCrawl(client, delay)
        collections = await cc.collections(snapshot_count)
        for pattern in PATTERNS:
            provider_sources: set[str] = set()
            for collection in collections:
                for query in pattern.queries:
                    try:
                        urls = await cc.urls(collection, query, max_pages)
                    except (httpx.HTTPError, ValueError) as exc:
                        errors.append({"provider": pattern.provider, "collection": collection, "query": query, "error": repr(exc)})
                        continue
                    completed += 1
                    for collector in extract_collectors(urls, pattern):
                        provider_sources.add(collector.name)
                        found[collector.name] = collector
            provider_counts[pattern.provider] = len(provider_sources)
    rows = serialize_collectors(list(found.values()))
    cap = max(100, int(os.getenv("GAIA_CENSUS_MAX_CANDIDATES", "100000")))
    rows = rows[:cap]
    return {
        "version": CENSUS_VERSION,
        "source": "common-crawl",
        "collections": collections,
        "summary": {
            "providers": len(PATTERNS),
            "queries_completed": completed,
            "query_errors": len(errors),
            "candidate_sources": len(rows),
            "candidate_sources_by_provider": dict(sorted(provider_counts.items())),
            "truncated": len(found) > len(rows),
        },
        "errors": errors[:100],
        "candidates": rows,
    }


def capture_snapshot(snapshot: dict[str, Any], batch_size: int) -> dict[str, Any]:
    if int(snapshot.get("version") or 0) != CENSUS_VERSION:
        raise ValueError("unsupported ATS census snapshot version")
    raw_rows = snapshot.get("candidates")
    if not isinstance(raw_rows, list):
        raise ValueError("ATS census snapshot is missing candidates")
    candidates = deserialize_collectors([row for row in raw_rows if isinstance(row, dict)])
    database = LiveDatabase(migrate=False)
    with database.connect() as connection:
        known = {str(row["source"]) for row in connection.execute("SELECT source FROM source_catalog WHERE validated").fetchall()}
        connection.execute("DELETE FROM source_candidates AS c USING source_catalog AS s WHERE c.source=s.source AND s.validated")
    unknown = [item for item in candidates if canonical_source_name(item.name) not in known]
    chunk = max(25, min(batch_size, 1000))
    written = 0
    for start in range(0, len(unknown), chunk):
        written += save_candidates(database, unknown[start : start + chunk], origin="common-crawl-ats-census")
    summary = dict(snapshot.get("summary") or {})
    summary.update({"candidate_rows_in_snapshot": len(candidates), "candidate_rows_already_validated": len(candidates) - len(unknown), "candidate_rows_written": written, "candidate_validation_deferred": True})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover ATS tenants from Common Crawl")
    parser.add_argument("--snapshot-count", type=int, default=int(os.getenv("GAIA_CENSUS_SNAPSHOTS", "2")))
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("GAIA_CENSUS_MAX_PAGES", "12")))
    parser.add_argument("--delay-seconds", type=float, default=float(os.getenv("GAIA_CENSUS_DELAY_SECONDS", "1.0")))
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--snapshot-output", type=Path)
    parser.add_argument("--snapshot-input", type=Path)
    parser.add_argument("--capture-only", action="store_true")
    args = parser.parse_args()
    if args.snapshot_input:
        snapshot = json.loads(args.snapshot_input.read_text(encoding="utf-8"))
    else:
        if args.capture_only:
            raise ValueError("--capture-only requires --snapshot-input")
        snapshot = asyncio.run(build_snapshot(max(1, args.snapshot_count), max(1, args.max_pages), max(0.25, args.delay_seconds)))
    if args.snapshot_output:
        args.snapshot_output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = capture_snapshot(snapshot, args.batch_size) if args.capture_only else snapshot.get("summary") or {}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
