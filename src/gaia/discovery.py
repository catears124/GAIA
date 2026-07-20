from __future__ import annotations

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
    DatabricksIndexCollector,
    GoogleCareersCollector,
    GreenhouseCollector,
    LeverCollector,
    SchemaPageCollector,
    WorkdayCollector,
    parse_date,
)
from .config import load_sources
from .models import Posting

MARKDOWN_LINK_RE = re.compile(r"\[.*?\]\((https?://[^)]+)\)")
HTML_LINK_RE = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)
LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)
IMAGE_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp)(?:\?|$)", re.I)

BLOCKED_APPLICATION_HOSTS = {
    "github.com",
    "www.github.com",
    "simplify.jobs",
    "www.simplify.jobs",
    "speedyapply.com",
    "www.speedyapply.com",
    "discord.gg",
    "www.linkedin.com",
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
            self.name,
            postings,
            True,
            self.mode,
            len(payload),
            len(payload),
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
        postings = [posting for posting in parsed if not _is_known_board_landing(posting.apply_url)]
        discovery_postings = [
            posting for posting in parsed if _is_known_board_landing(posting.apply_url)
        ]
        return CollectorResult(
            self.name,
            postings,
            True,
            self.mode,
            len(parsed),
            None,
            status="loaded",
            discovery_postings=discovery_postings,
        )


def _application_score(url: str) -> int:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path.lower()
    if not parts.scheme.startswith("http") or not host or IMAGE_RE.search(path):
        return -10_000
    if host in BLOCKED_APPLICATION_HOSTS:
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
    if host.startswith("jobs.") or host.startswith("careers."):
        score += 80
    if path in {"", "/"}:
        score -= 100
    return score


def _choose_apply_url(urls: list[str]) -> str | None:
    candidates = [(score := _application_score(url), index, url) for index, url in enumerate(urls)]
    candidates = [item for item in candidates if item[0] > -9_000]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


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
        if len(columns) < 2 or set(columns[0]) <= {"-", ":"}:
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
    postings: list[Posting] = []
    seen: set[tuple[str, str, str]] = set()
    rows = [*_pipe_rows(body), *_html_rows(body)]
    for index, (company, title, location, urls) in enumerate(rows):
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
        posting = Posting(
            company=company,
            title=title,
            apply_url=apply_url,
            source=source,
            source_id=f"{index}:{apply_url}",
            locations=[location] if location else [],
            source_mode="registry" if registry else "universe-seed",
        )
        classified = classify(posting, source_confirms_2027=registry)
        if classified.target_match != "not_internship":
            postings.append(classified)
    return postings


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
    for source_index, raw_url in enumerate(settings.get("universe_seeds", [])):
        url = str(raw_url)
        source = _seed_name(source_index, url)
        try:
            response = await client.get(url)
            response.raise_for_status()
            if url.endswith(".json"):
                seed_postings: list[Posting] = []
                for item in response.json():
                    company = _clean_cell(str(item.get("company_name") or ""))
                    title = str(item.get("title") or "").strip()
                    apply_url = str(item.get("url") or "").strip()
                    if company and title and apply_url:
                        seed_postings.append(
                            Posting(
                                company=company,
                                title=title,
                                apply_url=apply_url,
                                source=source,
                                source_id=str(item.get("id") or apply_url),
                                source_mode="universe-seed",
                            )
                        )
            else:
                seed_postings = _document_postings(
                    response.text,
                    source=source,
                    registry=False,
                )
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
    greenhouse_hosts = {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
    if host not in greenhouse_hosts:
        return None
    embed_token = (parse_qs(parts.query).get("for") or [None])[0]
    if embed_token:
        token = str(embed_token).strip()
        if token and token.lower() not in {"jobs", "job", "embed", "apply"}:
            return token
    if segments:
        token = segments[0]
        if token not in {"jobs", "job", "embed", "apply"} and not token.isdigit():
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
    if host == "jobs.lever.co":
        return len(segments) == 1
    if host == "jobs.ashbyhq.com":
        return len(segments) == 1
    if ".myworkdayjobs.com" in host:
        return "/job/" not in path.lower()
    return False


def _workday_site(segments: list[str]) -> str | None:
    if not segments:
        return None
    boundary = segments.index("job") if "job" in segments else len(segments)
    candidates = segments[:boundary]
    for candidate in reversed(candidates):
        if not LOCALE_RE.fullmatch(candidate) and candidate.lower() not in {"search", "jobs"}:
            return candidate
    return None


T = TypeVar("T")


def _register(
    mapping: dict[T, tuple[str, str]],
    key: T,
    company: str,
    scope: str,
) -> None:
    existing = mapping.get(key)
    if existing is None or (existing[1] == "historical" and scope == "current"):
        mapping[key] = (company, scope)


def _scoped(collector: Collector, scope: str) -> Collector:
    collector.scope = scope
    return collector


def collectors_from_registry(
    postings: list[Posting],
    settings: dict[str, Any] | None = None,
) -> list[Collector]:
    settings = settings or load_sources()
    greenhouse: dict[str, tuple[str, str]] = {}
    lever: dict[str, tuple[str, str]] = {}
    ashby: dict[str, tuple[str, str]] = {}
    workday: dict[tuple[str, str, str], tuple[str, str]] = {}
    schema_pages: dict[tuple[str, str], set[str]] = defaultdict(set)
    include_google = False
    include_databricks = False

    for posting in postings:
        url = posting.apply_url
        parts = urlsplit(url)
        host = parts.netloc.lower()
        segments = [value for value in parts.path.split("/") if value]
        scope = "historical" if posting.source_mode == "universe-seed" else "current"

        token = _greenhouse_token(url)
        if token:
            _register(greenhouse, token, posting.company, scope)
        elif host == "jobs.lever.co" and segments:
            _register(lever, segments[0], posting.company, scope)
        elif host == "jobs.ashbyhq.com" and segments:
            _register(ashby, segments[0], posting.company, scope)
        elif ".myworkdayjobs.com" in host and (site := _workday_site(segments)):
            tenant = host.split(".", 1)[0]
            root = f"{parts.scheme}://{host}"
            _register(workday, (root, tenant, site), posting.company, scope)
        elif scope == "historical":
            continue
        elif host in {"www.google.com", "google.com"} and "/about/careers/" in parts.path:
            include_google = True
        elif "databricks.com" in host:
            include_databricks = True
            schema_pages[(posting.company, host)].add(url)
        elif host:
            schema_pages[(posting.company, host)].add(url)

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
        _scoped(WorkdayCollector(company, root, tenant, site), scope)
        for (root, tenant, site), (company, scope) in workday.items()
    )
    collectors.extend(
        SchemaPageCollector(company, sorted(urls), name=f"schema:{host}:{company}")
        for (company, host), urls in schema_pages.items()
        if host and len(urls) <= 50
    )

    canaries = settings.get("release_canaries", {})
    if include_google or canaries.get("google", {}).get("enabled", False):
        collectors.append(GoogleCareersCollector())
    if include_databricks or canaries.get("databricks", {}).get("enabled", False):
        collectors.append(DatabricksIndexCollector())

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
