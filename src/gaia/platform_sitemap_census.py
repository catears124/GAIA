from __future__ import annotations

import argparse
import asyncio
import html
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
from .live_inventory import LiveDatabase
from .quality import canonical_source_name
from .source_catalog import _collector, _spec, save_candidates

SITEMAP_CENSUS_VERSION = 1
RETRYABLE = {429, 500, 502, 503, 504}
SLUG = r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?"
SUBDOMAIN_DENY = frozenset(
    {"admin", "api", "app", "assets", "auth", "cdn", "feeds", "login", "static", "www"}
)
LOC_RE = re.compile(r"<loc\b[^>]*>\s*([^<]{1,2048}?)\s*</loc>", re.I)


@dataclass(frozen=True, slots=True)
class SitemapSource:
    provider: str
    urls: tuple[str, ...]
    regex: re.Pattern[str]
    seed: str


SOURCES = (
    SitemapSource(
        "isolvedhire",
        ("https://feeds.isolvedhire.com/site_map_index.xml",),
        re.compile(rf"https?://({SLUG})\.isolvedhire\.com(?:[/:?#]|$)", re.I),
        "https://{slug}.isolvedhire.com",
    ),
    SitemapSource(
        "jazzhr",
        tuple(f"https://app.jazz.co/feeds/google/xml/{page}" for page in range(5)),
        re.compile(rf"https?://({SLUG})\.applytojob\.com(?:[/:?#]|$)", re.I),
        "https://{slug}.applytojob.com",
    ),
)


def parse_locs(body: str) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for match in LOC_RE.finditer(body):
        value = html.unescape(match.group(1).strip())
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def extract_slugs(urls: list[str], source: SitemapSource) -> list[str]:
    found: set[str] = set()
    for url in urls:
        match = source.regex.search(url)
        if not match:
            continue
        slug = match.group(1).casefold()
        if slug and slug not in SUBDOMAIN_DENY:
            found.add(slug)
    return sorted(found)


def collectors_for(source: SitemapSource, slugs: list[str]) -> list[CareerSurfaceCollector]:
    collectors: list[CareerSurfaceCollector] = []
    for slug in slugs:
        seed = source.seed.format(slug=slug)
        collector = CareerSurfaceCollector(
            " ".join(part.capitalize() for part in slug.replace("-", " ").split()) or slug,
            urlsplit(seed).netloc,
            [seed],
        )
        collector.name = f"domain:{source.provider}:{slug}"
        collectors.append(collector)
    return collectors


def serialize_collectors(collectors: list[CareerSurfaceCollector]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for collector in collectors:
        described = _spec(collector)
        source = canonical_source_name(collector.name)
        if described is None or not source:
            continue
        kind, spec = described
        rows.append(
            {
                "source": source,
                "kind": kind,
                "scope": collector.scope,
                "spec": spec,
            }
        )
    return rows


def deserialize_collectors(rows: list[dict[str, Any]]) -> list[CareerSurfaceCollector]:
    found: dict[str, CareerSurfaceCollector] = {}
    for row in rows:
        source = canonical_source_name(str(row.get("source") or ""))
        spec = row.get("spec")
        if not source or str(row.get("kind") or "") != "domain" or not isinstance(spec, dict):
            continue
        try:
            collector = _collector("domain", spec)
        except (KeyError, TypeError, ValueError):
            continue
        if not isinstance(collector, CareerSurfaceCollector):
            continue
        collector.name = source
        collector.scope = str(row.get("scope") or "current")
        found[source] = collector
    return list(found.values())


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    attempts = max(1, int(os.getenv("GAIA_SITEMAP_CENSUS_HTTP_RETRIES", "4")))
    response: httpx.Response | None = None
    for attempt in range(attempts):
        response = await client.get(url)
        if response.status_code not in RETRYABLE:
            response.raise_for_status()
            return response
        if attempt + 1 < attempts:
            raw = response.headers.get("Retry-After")
            try:
                delay = float(raw) if raw else 2.0 ** (attempt + 1)
            except ValueError:
                delay = 2.0 ** (attempt + 1)
            await asyncio.sleep(min(30.0, max(1.0, delay)))
    assert response is not None
    response.raise_for_status()
    return response


async def build_snapshot() -> dict[str, Any]:
    timeout = httpx.Timeout(float(os.getenv("GAIA_SITEMAP_CENSUS_HTTP_TIMEOUT", "45")))
    headers = {
        "User-Agent": os.getenv(
            "GAIA_USER_AGENT",
            "GAIA/5.0 platform-sitemap-census (+https://github.com/catears124/GAIA)",
        )
    }
    found: dict[str, CareerSurfaceCollector] = {}
    counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    requests = 0
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for source in SOURCES:
            provider_urls: list[str] = []
            for url in source.urls:
                try:
                    response = await _get(client, url)
                except httpx.HTTPError as exc:
                    errors.append({"provider": source.provider, "url": url, "error": repr(exc)})
                    continue
                requests += 1
                provider_urls.extend(parse_locs(response.text))
            slugs = extract_slugs(provider_urls, source)
            counts[source.provider] = len(slugs)
            for collector in collectors_for(source, slugs):
                found[collector.name] = collector

    rows = serialize_collectors(list(found.values()))
    return {
        "version": SITEMAP_CENSUS_VERSION,
        "source": "platform-sitemaps",
        "summary": {
            "providers": len(SOURCES),
            "requests_completed": requests,
            "request_errors": len(errors),
            "candidate_sources": len(rows),
            "candidate_sources_by_provider": dict(sorted(counts.items())),
        },
        "errors": errors[:100],
        "candidates": rows,
    }


def capture_snapshot(snapshot: dict[str, Any], batch_size: int) -> dict[str, Any]:
    if int(snapshot.get("version") or 0) != SITEMAP_CENSUS_VERSION:
        raise ValueError("unsupported platform sitemap census snapshot version")
    raw_rows = snapshot.get("candidates")
    if not isinstance(raw_rows, list):
        raise ValueError("platform sitemap census snapshot is missing candidates")
    candidates = deserialize_collectors([row for row in raw_rows if isinstance(row, dict)])
    database = LiveDatabase(migrate=False)
    with database.connect() as connection:
        known = {
            str(row["source"])
            for row in connection.execute(
                "SELECT source FROM source_catalog WHERE validated"
            ).fetchall()
        }
        connection.execute(
            """
            DELETE FROM source_candidates AS c
            USING source_catalog AS s
            WHERE c.source=s.source AND s.validated
            """
        )
    unknown = [
        item for item in candidates if canonical_source_name(item.name) not in known
    ]
    chunk = max(25, min(batch_size, 1000))
    written = 0
    for start in range(0, len(unknown), chunk):
        written += save_candidates(
            database,
            unknown[start : start + chunk],
            origin="platform-sitemap-census",
        )
    summary = dict(snapshot.get("summary") or {})
    summary.update(
        {
            "candidate_rows_in_snapshot": len(candidates),
            "candidate_rows_already_validated": len(candidates) - len(unknown),
            "candidate_rows_written": written,
            "candidate_validation_deferred": True,
        }
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover ATS tenants from public platform sitemap/feed indexes"
    )
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
        snapshot = asyncio.run(build_snapshot())

    if args.snapshot_output:
        args.snapshot_output.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    result = capture_snapshot(snapshot, args.batch_size) if args.capture_only else snapshot.get("summary") or {}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
