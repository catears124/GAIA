from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from .classify import classify
from .models import CollectorResult, Posting, canonical_url
from .page_verification import page_is_closed, posting_from_unstructured_page


def parse_date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, tz=UTC)
    try:
        parsed = date_parser.parse(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def text(value: Any) -> str:
    return str(value or "").strip()


def locations_from(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            if isinstance(item, dict):
                candidate = item.get("name") or item.get("addressLocality") or item.get("location")
                if candidate:
                    output.append(str(candidate).strip())
            elif item:
                output.append(str(item).strip())
        return output
    if isinstance(value, dict):
        candidate = value.get("name") or value.get("addressLocality") or value.get("location")
        return [str(candidate).strip()] if candidate else []
    return []


def json_ld_jobs(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            kind = node.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                jobs.append(node)
            for key in ("@graph", "itemListElement"):
                if key in node:
                    visit(node[key])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            continue
    return jobs


def posting_from_schema(
    job: dict[str, Any], *, source: str, source_mode: str = "direct"
) -> Posting | None:
    title = text(job.get("title"))
    url = text(job.get("url") or job.get("sameAs"))
    identifier = job.get("identifier")
    if isinstance(identifier, dict):
        source_id = text(identifier.get("value") or identifier.get("name"))
    else:
        source_id = text(identifier)
    company_value = job.get("hiringOrganization")
    company = text(company_value.get("name")) if isinstance(company_value, dict) else text(company_value)
    if not title or not url:
        return None
    source_id = source_id or url
    location_value = job.get("jobLocation") or job.get("applicantLocationRequirements")
    locations: list[str] = []
    if isinstance(location_value, list):
        for item in location_value:
            if isinstance(item, dict):
                address = item.get("address", item)
                if isinstance(address, dict):
                    joined = ", ".join(
                        text(address.get(field))
                        for field in ("addressLocality", "addressRegion", "addressCountry")
                        if text(address.get(field))
                    )
                    if joined:
                        locations.append(joined)
    elif isinstance(location_value, dict):
        address = location_value.get("address", location_value)
        if isinstance(address, dict):
            joined = ", ".join(
                text(address.get(field))
                for field in ("addressLocality", "addressRegion", "addressCountry")
                if text(address.get(field))
            )
            if joined:
                locations.append(joined)
    posted = parse_date(job.get("datePosted"))
    return classify(
        Posting(
            company=company or source,
            title=title,
            apply_url=url,
            source=source,
            source_id=source_id,
            locations=locations,
            source_mode=source_mode,
            description=text(job.get("description")),
            employment_type=text(job.get("employmentType")),
            posted_at=posted,
            posted_raw=text(job.get("datePosted")) or None,
            posted_precision="timestamp" if posted else "unknown",
            posted_confidence="structured" if posted else "unknown",
        )
    )


class Collector(ABC):
    name: str
    mode: str = "board"
    scope: str = "current"

    @abstractmethod
    async def collect(self, client: httpx.AsyncClient) -> CollectorResult: ...


class GreenhouseCollector(Collector):
    mode = "board"

    def __init__(self, company: str, board: str) -> None:
        self.company = company
        self.board = board
        self.name = f"greenhouse:{board}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.board}/jobs"
        response = await client.get(url, params={"content": "true"})
        response.raise_for_status()
        payload = response.json()
        postings: list[Posting] = []
        for job in payload.get("jobs", []):
            offices = job.get("offices") or []
            locations = locations_from(offices) or locations_from(job.get("location"))
            posted = parse_date(job.get("first_published"))
            postings.append(
                classify(
                    Posting(
                        company=self.company,
                        title=text(job.get("title")),
                        apply_url=text(job.get("absolute_url")),
                        source=self.name,
                        source_id=text(job.get("id")),
                        locations=locations,
                        description=text(job.get("content")),
                        posted_at=posted,
                        updated_at=parse_date(job.get("updated_at")),
                        posted_raw=text(job.get("first_published")) or None,
                        posted_precision="timestamp" if posted else "unknown",
                        posted_confidence="official" if posted else "unknown",
                    )
                )
            )
        return CollectorResult(self.name, postings, True, self.mode, len(postings), len(postings))


class LeverCollector(Collector):
    mode = "board"

    def __init__(self, company: str, site: str) -> None:
        self.company = company
        self.site = site
        self.name = f"lever:{site}"

    async def _enrich_date(self, client: httpx.AsyncClient, posting: Posting) -> Posting:
        if posting.posted_at or not posting.apply_url:
            return posting
        try:
            response = await client.get(posting.apply_url)
            response.raise_for_status()
        except httpx.HTTPError:
            return posting
        schemas = json_ld_jobs(response.text)
        if not schemas:
            return posting
        matching = next(
            (
                job
                for job in schemas
                if text(job.get("url") or job.get("sameAs")).rstrip("/")
                == posting.apply_url.rstrip("/")
            ),
            schemas[0],
        )
        posted_raw = text(matching.get("datePosted"))
        posted = parse_date(posted_raw)
        if posted:
            posting.posted_at = posted
            posting.posted_raw = posted_raw
            posting.posted_precision = "timestamp"
            posting.posted_confidence = "structured"
        posting.description = text(matching.get("description")) or posting.description
        posting.employment_type = text(matching.get("employmentType")) or posting.employment_type
        return classify(posting)

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(
            f"https://api.lever.co/v0/postings/{self.site}", params={"mode": "json"}
        )
        response.raise_for_status()
        payload = response.json()
        postings = [
            classify(
                Posting(
                    company=self.company,
                    title=text(job.get("text")),
                    apply_url=text(job.get("hostedUrl") or job.get("applyUrl")),
                    source=self.name,
                    source_id=text(job.get("id")),
                    locations=locations_from((job.get("categories") or {}).get("location")),
                    description=text(job.get("descriptionPlain")),
                    employment_type=text((job.get("categories") or {}).get("commitment")),
                )
            )
            for job in payload
        ]
        enrichable = [
            item
            for item in postings
            if item.target_match != "not_internship" and item.category != "other"
        ]
        semaphore = asyncio.Semaphore(8)

        async def enrich(item: Posting) -> Posting:
            async with semaphore:
                return await self._enrich_date(client, item)

        enriched = await asyncio.gather(*(enrich(item) for item in enrichable))
        by_key = {item.posting_key: item for item in enriched}
        postings = [by_key.get(item.posting_key, item) for item in postings]
        return CollectorResult(self.name, postings, True, self.mode, len(payload), len(payload))


class AshbyCollector(Collector):
    mode = "board"

    def __init__(self, company: str, board: str) -> None:
        self.company = company
        self.board = board
        self.name = f"ashby:{board}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{self.board}",
            params={"includeCompensation": "true"},
        )
        response.raise_for_status()
        jobs = response.json().get("jobs", [])
        postings = []
        for job in jobs:
            posted = parse_date(job.get("publishedAt"))
            postings.append(
                classify(
                    Posting(
                        company=self.company,
                        title=text(job.get("title")),
                        apply_url=text(job.get("jobUrl") or job.get("applyUrl")),
                        source=self.name,
                        source_id=text(job.get("id") or job.get("jobUrl")),
                        locations=locations_from(job.get("location"))
                        + locations_from(job.get("secondaryLocations")),
                        description=text(job.get("descriptionPlain") or job.get("descriptionHtml")),
                        employment_type=text(job.get("employmentType")),
                        posted_at=posted,
                        updated_at=parse_date(job.get("updatedAt")),
                        posted_raw=text(job.get("publishedAt")) or None,
                        posted_precision="timestamp" if posted else "unknown",
                        posted_confidence="official" if posted else "unknown",
                    )
                )
            )
        return CollectorResult(self.name, postings, True, self.mode, len(jobs), len(jobs))


class SchemaPageCollector(Collector):
    mode = "verification"

    def __init__(
        self,
        company: str,
        urls: list[str] | None = None,
        name: str | None = None,
        *,
        leads: list[Posting] | None = None,
        trusted: bool = True,
    ) -> None:
        self.company = company
        self.leads = list(leads or [])
        self.source_mode = "verification" if trusted else "verification-lead"
        lead_urls = [item.apply_url for item in self.leads]
        self.urls = list(dict.fromkeys([*(urls or []), *lead_urls]))
        if not self.urls:
            raise ValueError("SchemaPageCollector requires at least one URL")
        self.leads_by_url = {item.canonical_apply_url: item for item in self.leads}
        self.name = name or f"schema:{urlsplit(self.urls[0]).netloc}"
        self.fetch_concurrency = max(1, int(os.getenv("GAIA_VERIFY_CONCURRENCY", "12")))

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        semaphore = asyncio.Semaphore(self.fetch_concurrency)

        async def fetch(url: str) -> tuple[str, list[Posting], str | None, str | None]:
            async with semaphore:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code in {404, 410}:
                        return "stale", [], canonical_url(url), None
                    if code in {401, 403, 429}:
                        return "blocked", [], None, None
                    return "error", [], None, f"{code} {url}"
                except httpx.HTTPError as exc:
                    return "error", [], None, f"{type(exc).__name__}: {url}"

                if page_is_closed(response.text):
                    return "stale", [], canonical_url(url), None

                jobs = json_ld_jobs(response.text)
                if jobs:
                    parsed_postings: list[Posting] = []
                    for job in jobs:
                        parsed = posting_from_schema(
                            job,
                            source=self.name,
                            source_mode=self.source_mode,
                        )
                        if parsed:
                            parsed.company = self.company
                            parsed_postings.append(classify(parsed))
                    if parsed_postings:
                        return "verified", parsed_postings, None, None

                lead = self.leads_by_url.get(canonical_url(url))
                fallback = posting_from_unstructured_page(
                    response.text,
                    page_url=str(response.url),
                    company=self.company,
                    source=self.name,
                    lead=lead,
                )
                if fallback:
                    fallback.source_mode = self.source_mode
                    return "verified", [fallback], None, None
                return "unstructured", [], None, None

        results = await asyncio.gather(*(fetch(url) for url in self.urls))
        postings: list[Posting] = []
        closed_urls: list[str] = []
        blocked = stale = unstructured = 0
        hard_errors: list[str] = []
        for status, recovered, closed_url, error in results:
            postings.extend(recovered)
            if closed_url:
                closed_urls.append(closed_url)
            if status == "blocked":
                blocked += 1
            elif status == "stale":
                stale += 1
            elif status == "unstructured":
                unstructured += 1
            elif status == "error" and error:
                hard_errors.append(error)

        postings = list({item.canonical_apply_url: item for item in postings}.values())
        notes: list[str] = []
        if stale:
            notes.append(f"{stale} stale/closed page{'s' if stale != 1 else ''}")
        if blocked:
            notes.append(f"{blocked} access-blocked page{'s' if blocked != 1 else ''}")
        if unstructured:
            notes.append(
                f"{unstructured} reachable page{'s' if unstructured != 1 else ''} without sufficient job evidence"
            )
        if hard_errors:
            notes.append(
                f"{len(hard_errors)} transport/server failure{'s' if len(hard_errors) != 1 else ''}"
            )

        if hard_errors:
            status = "partial" if postings or stale or blocked or unstructured else "broken"
            error = "; ".join(hard_errors[:3])
        elif postings and (stale or blocked or unstructured):
            status = "partial"
            error = None
        elif postings:
            status = "verified"
            error = None
        elif blocked and not stale and not unstructured:
            status = "blocked"
            error = None
        elif stale and not blocked and not unstructured:
            status = "stale"
            error = None
        elif unstructured and not blocked and not stale:
            status = "unstructured"
            error = None
        else:
            status = "mixed"
            error = None

        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=False,
            mode=self.mode,
            rows_scanned=len(self.urls),
            expected_rows=len(self.urls),
            error=error,
            status=status,
            note="; ".join(notes) or None,
            closed_urls=closed_urls,
        )


class GoogleCareersCollector(Collector):
    """Compatibility factory for the native Google internship-search collector."""

    name = "google-careers"
    mode = "board-search"

    def __new__(cls, pages: int = 10):
        from .native_collectors import GoogleInternshipCollector

        return GoogleInternshipCollector(pages=pages)

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        raise RuntimeError("GoogleCareersCollector is a compatibility factory")
