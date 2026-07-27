from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from bs4 import BeautifulSoup

from .classify import classify
from .collectors import (
    AshbyCollector,
    Collector,
    CollectorResult,
    GoogleCareersCollector,
    GreenhouseCollector,
    LeverCollector,
    SchemaPageCollector,
    parse_date,
)
from .config import load_sources
from .market_collectors import SitemapDomainCollector, WorkdaySearchCollector
from .models import Posting
from .quality import is_actionable_application_url

MARKDOWN_LINK_RE = re.compile(r"\[.*?\]\((https?://[^)]+)\)")
HTML_LINK_RE = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)
LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)
IMAGE_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp)(?:\?|$)", re.I)

UNTRUSTED_VERIFICATION_HOSTS = {
    "jobright.ai",
    "www.jobright.ai",
    "workopia.io",
    "www.workopia.io",
    "indeed.com",
    "www.indeed.com",
    "glassdoor.com",
    "www.glassdoor.com",
    "ziprecruiter.com",
    "www.ziprecruiter.com",
}
KNOWN_ATS_FRAGMENTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "oraclecloud.com",
    "icims.com",
    "jobvite.com",
    "workable.com",
    "recruitee.com",
    "rippling.com",
)


class JsonRegistryCollector(Collector):
    mode = "registry"

    def __init__(self, name: str, url: str) -> None:
        self.name = f"registry:{name}"
        self.url = url

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(self.url)
        response.raise_for_status()
        payload = response.json()
        postings: list[Posting] = []
        discovery_postings: list[Posting] = []
        for item in payload:
            if not item.get("active", True) or not item.get("is_visible", True):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            company = _clean_cell(str(item.get("company_name") or ""))
            if not url or not title or not company:
                continue
            posting = classify(
                Posting(
                    company=company,
                    title=title,
                    apply_url=url,
                    source=self.name,
                    source_id=str(item.get("id") or url),
                    locations=[str(value) for value in item.get("locations") or []],
                    source_mode="registry",
                    posted_at=parse_date(item.get("date_posted")),
                    posted_raw=str(item.get("date_posted") or "") or None,
                    posted_precision="timestamp" if item.get("date_posted") else "unknown",
                    posted_confidence="registry-reported" if item.get("date_posted") else "unknown",
                ),
                source_confirms_2027=True,
            )
            if _is_known_board_landing(posting.apply_url):
                discovery_postings.append(posting)
            else:
                postings.append(posting)
        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=True,
            mode=self.mode,
            rows_scanned=len(payload),
            expected_rows=len(payload),
            status="loaded",
            discovery_postings=discovery_postings,
        )


class MarkdownRegistryCollector(Collector):
    mode = "registry"

    def __init__(self, name: str, url: str) -> None:
        self.name = f"registry:{name}"
        self.url = url

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(self.url)
        response.raise_for_status()
        parsed = _document_postings(response.text, source=self.name, registry=True)
        postings = [item for item in parsed if not _is_known_board_landing(item.apply_url)]
        discovery_postings = [item for item in parsed if _is_known_board_landing(item.apply_url)]
        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=True,
            mode=self.mode,
            rows_scanned=len(parsed),
            status="loaded",
            discovery_postings=discovery_postings,
        )


def _application_score(url: str) -> int:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path.lower()
    if not parts.scheme.startswith("http") or not host or IMAGE_RE.search(path):
        return -10_000
    if not is_actionable_application_url(url):
        return -9_000
    score = 0
    if any(fragment in host for fragment in KNOWN_ATS_FRAGMENTS):
        score += 500
    if any(
        marker in path
        for marker in (
            "/job/",
            "/jobs/",
            "/position/",
            "/positions/",
            "/careers/",
            "/apply",
            "/details/",
            "/results/",
            "/search/",
        )
    ):
        score += 200
    if "gh_jid=" in url or "job_id=" in url:
        score += 250
    if host.startswith(("jobs.", "careers.")):
        score += 80
    if path in {"", "/"}:
        score -= 100
    return score


def _choose_apply_url(urls: list[str]) -> str | None:
    candidates = [
        (score, index, url)
        for index, url in enumerate(urls)
        if (score := _application_score(url)) > -9_000
    ]
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def _clean_cell(value: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip(" *\t")


def _pipe_rows(body: str) -> list[tuple[str, str, str, list[str]]]:
    rows: list[tuple[str, str, str, list[str]]] = []
    previous_company = ""
    for line in body.splitlines():
        if not line.lstrip().startswith("|") or "intern" not in line.lower():
            continue
        columns = [_clean_cell(value) for value in line.split("|")[1:-1]]
        if len(columns) < 2 or not columns[0] or set(columns[0]) <= {"-", ":"}:
            continue
        company = columns[0]
        if company in {"", "↳", "—", "-"}:
            company = previous_company
        elif company.lower() not in {"company", "employer"}:
            previous_company = company
        title = columns[1]
        location = columns[2] if len(columns) > 2 else ""
        urls = MARKDOWN_LINK_RE.findall(line) + HTML_LINK_RE.findall(line)
        if company and title:
            rows.append((company, title, location, urls))
    return rows


def _html_rows(body: str) -> list[tuple[str, str, str, list[str]]]:
    soup = BeautifulSoup(body, "html.parser")
    rows: list[tuple[str, str, str, list[str]]] = []
    previous_company = ""
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        values = [_clean_cell(cell.get_text(" ", strip=True)) for cell in cells]
        if "intern" not in " ".join(values).lower():
            continue
        company = values[0]
        if company in {"", "↳", "—", "-"}:
            company = previous_company
        elif company.lower() not in {"company", "employer"}:
            previous_company = company
        title = values[1]
        location = values[2] if len(values) > 2 else ""
        urls = [str(anchor.get("href")) for anchor in row.find_all("a", href=True)]
        if company and title:
            rows.append((company, title, location, urls))
    return rows


def _document_postings(body: str, *, source: str, registry: bool) -> list[Posting]:
    output: list[Posting] = []
    seen: set[tuple[str, str, str]] = set()
    for index, (company, title, location, urls) in enumerate([*_pipe_rows(body), *_html_rows(body)]):
        company = _clean_cell(company)
        title = _clean_cell(title)
        location = _clean_cell(location)
        apply_url = _choose_apply_url(urls)
        if not apply_url:
            continue
        identity = (company.casefold(), title.casefold(), apply_url)
        if identity in seen:
            continue
        seen.add(identity)
        item = classify(
            Posting(
                company=company,
                title=title,
                apply_url=apply_url,
                source=source,
                source_id=f"{index}:{apply_url}",
                locations=[location] if location else [],
                source_mode="registry" if registry else "universe-seed",
            ),
            source_confirms_2027=registry,
        )
        if item.target_match != "not_internship":
            output.append(item)
    return output


def registry_collectors(settings: dict[str, Any] | None = None) -> list[Collector]:
    settings = settings or load_sources()
    collectors: list[Collector] = []
    for item in settings.get("target_registries", []):
        kind = item.get("kind")
        if kind == "json":
            collectors.append(JsonRegistryCollector(str(item["name"]), str(item["url"])))
        elif kind == "markdown":
            collectors.append(MarkdownRegistryCollector(str(item["name"]), str(item["url"])))
    return collectors


def _seed_name(index: int, url: str) -> str:
    parts = urlsplit(url)
    basename = PurePosixPath(parts.path).name or "feed"
    return f"universe-seed:{index}:{parts.netloc}:{basename}"


async def load_universe_seed_postings(
    client: httpx.AsyncClient,
    settings: dict[str, Any] | None = None,
) -> tuple[list[Posting], list[CollectorResult]]:
    settings = settings or load_sources()
    output: list[Posting] = []
    health: list[CollectorResult] = []
    for index, raw_url in enumerate(settings.get("universe_seeds", [])):
        url = str(raw_url)
        source = _seed_name(index, url)
        try:
            response = await client.get(url)
            response.raise_for_status()
            if url.endswith(".json"):
                seed_postings = [
                    Posting(
                        company=_clean_cell(str(item.get("company_name") or "")),
                        title=str(item.get("title") or "").strip(),
                        apply_url=str(item.get("url") or "").strip(),
                        source=source,
                        source_id=str(item.get("id") or item.get("url") or ""),
                        source_mode="universe-seed",
                    )
                    for item in response.json()
                    if item.get("company_name") and item.get("title") and item.get("url")
                ]
            else:
                seed_postings = _document_postings(response.text, source=source, registry=False)
            output.extend(seed_postings)
            health.append(
                CollectorResult(
                    source=source,
                    postings=[],
                    complete=True,
                    mode="universe-seed",
                    rows_scanned=len(seed_postings),
                    expected_rows=len(seed_postings),
                    status="loaded",
                    scope="historical",
                )
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            health.append(
                CollectorResult(
                    source=source,
                    postings=[],
                    complete=False,
                    mode="universe-seed",
                    rows_scanned=0,
                    error=repr(exc),
                    status="broken",
                    scope="historical",
                )
            )
    return output, health


def _greenhouse_token(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    segments = [value for value in parts.path.split("/") if value]
    if host not in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }:
        return None
    embed_token = (parse_qs(parts.query).get("for") or [None])[0]
    if embed_token:
        token = str(embed_token).strip()
        if token and token.lower() not in {"jobs", "job", "embed", "apply"}:
            return token
    if segments:
        token = segments[0]
        if token.lower() not in {"jobs", "job", "embed", "apply"} and not token.isdigit():
            return token
    return None


def _is_known_board_landing(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query)
    segments = [value for value in path.split("/") if value]
    if "greenhouse.io" in host:
        if query.get("gh_jid") or query.get("job_id") or query.get("token"):
            return False
        if re.search(r"/(?:jobs?|apply)/(?:\d+|[0-9a-f-]{20,})(?:/|$)", path, re.I):
            return False
        return _greenhouse_token(url) is not None
    if host in {"jobs.lever.co", "jobs.ashbyhq.com"}:
        return len(segments) == 1
    if ".myworkdayjobs.com" in host:
        return "/job/" not in path.lower()
    return False


def _workday_site(segments: list[str]) -> str | None:
    if not segments:
        return None
    boundary = segments.index("job") if "job" in segments else len(segments)
    for candidate in reversed(segments[:boundary]):
        if not LOCALE_RE.fullmatch(candidate) and candidate.lower() not in {"search", "jobs"}:
            return candidate
    return None


T = TypeVar("T")


def _register(mapping: dict[T, tuple[str, str]], key: T, company: str, scope: str) -> None:
    existing = mapping.get(key)
    if existing is None or (existing[1] == "historical" and scope == "current"):
        mapping[key] = (company, scope)


def _scoped(collector: Collector, scope: str) -> Collector:
    collector.scope = scope
    return collector


def collectors_from_registry(
    postings: list[Posting],
    settings: dict[str, Any] | None = None,
    *,
    deep: bool = False,
) -> list[Collector]:
    settings = settings or load_sources()
    greenhouse: dict[str, tuple[str, str]] = {}
    lever: dict[str, tuple[str, str]] = {}
    ashby: dict[str, tuple[str, str]] = {}
    workday: dict[tuple[str, str, str], tuple[str, str]] = {}
    custom_pages: dict[tuple[str, str], dict[str, Posting]] = defaultdict(dict)
    include_google = False

    for posting in postings:
        parts = urlsplit(posting.apply_url)
        host = parts.netloc.lower()
        segments = [value for value in parts.path.split("/") if value]
        scope = "historical" if posting.source_mode == "universe-seed" else "current"
        if token := _greenhouse_token(posting.apply_url):
            _register(greenhouse, token, posting.company, scope)
        elif parse_qs(parts.query).get("gh_jid"):
            inferred_board = re.sub(r"[^a-z0-9]", "", posting.company.lower())
            if inferred_board:
                _register(greenhouse, inferred_board, posting.company, scope)
        elif host == "jobs.lever.co" and segments:
            _register(lever, segments[0], posting.company, scope)
        elif host == "jobs.ashbyhq.com" and segments:
            _register(ashby, segments[0], posting.company, scope)
        elif ".myworkdayjobs.com" in host and (site := _workday_site(segments)):
            tenant = host.split(".", 1)[0]
            _register(workday, (f"{parts.scheme}://{host}", tenant, site), posting.company, scope)
        elif scope == "historical":
            continue
        elif host in {"www.google.com", "google.com"} and "/about/careers/" in parts.path:
            include_google = True
        elif host:
            custom_pages[(posting.company, host)][posting.canonical_apply_url] = posting

    terms = tuple(settings.get("workday", {}).get("search_terms") or ("intern", "co-op"))
    collectors: list[Collector] = []
    collectors.extend(
        _scoped(GreenhouseCollector(company, board), scope)
        for board, (company, scope) in greenhouse.items()
    )
    collectors.extend(
        _scoped(LeverCollector(company, site), scope)
        for site, (company, scope) in lever.items()
    )
    collectors.extend(
        _scoped(AshbyCollector(company, board), scope)
        for board, (company, scope) in ashby.items()
    )
    collectors.extend(
        _scoped(WorkdaySearchCollector(company, root, tenant, site, terms=terms), scope)
        for (root, tenant, site), (company, scope) in workday.items()
    )

    verify_batch_size = max(25, int(os.getenv("GAIA_VERIFY_BATCH_SIZE", "250")))
    for (company, host), lead_map in custom_pages.items():
        leads = list(lead_map.values())
        for batch_index in range(0, len(leads), verify_batch_size):
            batch = leads[batch_index : batch_index + verify_batch_size]
            suffix = f":{batch_index // verify_batch_size + 1}" if len(leads) > verify_batch_size else ""
            collectors.append(
                SchemaPageCollector(
                    company,
                    name=f"schema:{host}:{company}{suffix}",
                    leads=batch,
                    trusted=host not in UNTRUSTED_VERIFICATION_HOSTS,
                )
            )
        if deep:
            collectors.append(
                SitemapDomainCollector(company, host, [item.apply_url for item in leads])
            )

    native_kinds = {str(item.get("kind")) for item in settings.get("native_sources", [])}
    if include_google or "google-careers" in native_kinds:
        collectors.append(GoogleCareersCollector())

    unique: dict[str, Collector] = {}
    for collector in collectors:
        existing = unique.get(collector.name)
        if existing is None or (existing.scope == "historical" and collector.scope == "current"):
            unique[collector.name] = collector
    return list(unique.values())


def dump_discovery(postings: list[Posting]) -> dict[str, Any]:
    collectors = collectors_from_registry(postings)
    return {
        "seed_postings": len(postings),
        "collectors": [
            {"name": item.name, "mode": item.mode, "scope": item.scope}
            for item in collectors
        ],
    }
