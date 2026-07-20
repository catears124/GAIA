from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

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
        for item in payload:
            if not item.get("active", True) or not item.get("is_visible", True):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            company = str(item.get("company_name") or "").strip()
            if not url or not title or not company:
                continue
            posting = Posting(
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
            )
            postings.append(classify(posting, source_confirms_2027=True))
        return CollectorResult(self.name, postings, True, self.mode, len(payload), len(payload))


class MarkdownRegistryCollector(Collector):
    mode = "registry"

    def __init__(self, name: str, url: str) -> None:
        self.name = f"registry:{name}"
        self.url = url

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(self.url)
        response.raise_for_status()
        postings = _document_postings(response.text, source=self.name, registry=True)
        return CollectorResult(self.name, postings, True, self.mode, len(postings), None)


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
    # Later links commonly contain the actual application after a company homepage link.
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", value)).strip(" *\t")


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
        values = [" ".join(cell.get_text(" ", strip=True).split()) for cell in cells]
        if "intern" not in " ".join(values).lower():
            continue
        company = values[0].strip(" *")
        if company in {"", "↳", "—", "-"}:
            company = previous_company
        elif company.lower() not in {"company", "employer"}:
            previous_company = company
        title = values[1].strip()
        location = values[2].strip() if len(values) > 2 else ""
        urls = [str(anchor.get("href")) for anchor in row.find_all("a", href=True)]
        if company and title:
            rows.append((company, title, location, urls))
    return rows


def _document_postings(body: str, *, source: str, registry: bool) -> list[Posting]:
    postings: list[Posting] = []
    seen: set[tuple[str, str, str]] = set()
    rows = [*_pipe_rows(body), *_html_rows(body)]
    for index, (company, title, location, urls) in enumerate(rows):
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
                    company = str(item.get("company_name") or "").strip()
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
                )
            )
    return output, health


def _greenhouse_token(url: str) -> str | None:
    parts = urlsplit(url)
    segments = [value for value in parts.path.split("/") if value]
    greenhouse_hosts = {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
    if parts.netloc in greenhouse_hosts and segments:
        token = segments[0]
        if token not in {"jobs", "job", "embed", "apply"} and not token.isdigit():
            return token
    return None


def _workday_site(segments: list[str]) -> str | None:
    if not segments:
        return None
    boundary = segments.index("job") if "job" in segments else len(segments)
    candidates = segments[:boundary]
    for candidate in reversed(candidates):
        if not LOCALE_RE.fullmatch(candidate) and candidate.lower() not in {"search", "jobs"}:
            return candidate
    return None


def collectors_from_registry(
    postings: list[Posting],
    settings: dict[str, Any] | None = None,
) -> list[Collector]:
    settings = settings or load_sources()
    greenhouse: dict[str, str] = {}
    lever: dict[str, str] = {}
    ashby: dict[str, str] = {}
    workday: dict[tuple[str, str, str], str] = {}
    schema_pages: dict[tuple[str, str], set[str]] = defaultdict(set)
    include_google = False
    include_databricks = False

    for posting in postings:
        url = posting.apply_url
        parts = urlsplit(url)
        host = parts.netloc.lower()
        segments = [value for value in parts.path.split("/") if value]

        token = _greenhouse_token(url)
        if token:
            greenhouse[token] = posting.company
        elif host == "jobs.lever.co" and segments:
            lever[segments[0]] = posting.company
        elif host == "jobs.ashbyhq.com" and segments:
            ashby[segments[0]] = posting.company
        elif ".myworkdayjobs.com" in host and (site := _workday_site(segments)):
            tenant = host.split(".", 1)[0]
            root = f"{parts.scheme}://{host}"
            workday[(root, tenant, site)] = posting.company
        elif host in {"www.google.com", "google.com"} and "/about/careers/" in parts.path:
            include_google = True
        elif "databricks.com" in host:
            include_databricks = True
            schema_pages[(posting.company, host)].add(url)
        else:
            schema_pages[(posting.company, host)].add(url)

    collectors: list[Collector] = []
    collectors.extend(GreenhouseCollector(company, board) for board, company in greenhouse.items())
    collectors.extend(LeverCollector(company, site) for site, company in lever.items())
    collectors.extend(AshbyCollector(company, board) for board, company in ashby.items())
    collectors.extend(
        WorkdayCollector(company, root, tenant, site)
        for (root, tenant, site), company in workday.items()
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
        unique.setdefault(collector.name, collector)
    return list(unique.values())


def dump_discovery(postings: list[Posting]) -> dict[str, Any]:
    collectors = collectors_from_registry(postings)
    return {
        "seed_postings": len(postings),
        "collectors": [{"name": item.name, "mode": item.mode} for item in collectors],
    }
