from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

import httpx

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
        postings = _markdown_postings(response.text, source=self.name, registry=True)
        return CollectorResult(self.name, postings, True, self.mode, len(postings), None)


def _markdown_postings(body: str, *, source: str, registry: bool) -> list[Posting]:
    postings: list[Posting] = []
    for index, line in enumerate(body.splitlines()):
        if not line.lstrip().startswith("|") or "intern" not in line.lower():
            continue
        columns = [re.sub(r"<[^>]+>", "", value).strip(" *") for value in line.split("|")[1:-1]]
        urls = MARKDOWN_LINK_RE.findall(line) + HTML_LINK_RE.findall(line)
        if len(columns) < 2 or not urls:
            continue
        company = columns[0] or "Unknown"
        title = columns[1]
        location = columns[2] if len(columns) > 2 else ""
        posting = Posting(
            company=company,
            title=title,
            apply_url=urls[-1],
            source=source,
            source_id=f"{index}:{urls[-1]}",
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


async def load_universe_seed_postings(
    client: httpx.AsyncClient, settings: dict[str, Any] | None = None
) -> list[Posting]:
    settings = settings or load_sources()
    output: list[Posting] = []
    for source_index, url in enumerate(settings.get("universe_seeds", [])):
        try:
            response = await client.get(str(url))
            response.raise_for_status()
            if str(url).endswith(".json"):
                for item in response.json():
                    company = str(item.get("company_name") or "").strip()
                    title = str(item.get("title") or "").strip()
                    apply_url = str(item.get("url") or "").strip()
                    if company and title and apply_url:
                        output.append(
                            Posting(
                                company=company,
                                title=title,
                                apply_url=apply_url,
                                source=f"universe-seed:{source_index}",
                                source_id=str(item.get("id") or apply_url),
                                source_mode="universe-seed",
                            )
                        )
            else:
                output.extend(
                    _markdown_postings(
                        response.text,
                        source=f"universe-seed:{source_index}",
                        registry=False,
                    )
                )
        except (httpx.HTTPError, ValueError, TypeError):
            # Seed failures reduce discovery breadth but never invalidate current target rows.
            continue
    return output


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


def collectors_from_registry(
    postings: list[Posting], settings: dict[str, Any] | None = None
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
        elif ".myworkdayjobs.com" in host and segments:
            tenant = host.split(".", 1)[0]
            site = segments[0]
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
