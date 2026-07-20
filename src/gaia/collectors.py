from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from .classify import classify
from .models import CollectorResult, Posting

JOB_URL_RE = re.compile(r"/about/careers/applications/jobs/results/(\d+)(?:-[^?#/]+)?")


def parse_date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    try:
        parsed = date_parser.parse(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
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


def posting_from_schema(job: dict[str, Any], *, source: str, source_mode: str = "direct") -> Posting | None:
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

    @abstractmethod
    async def collect(self, client: httpx.AsyncClient) -> CollectorResult: ...


class GoogleCareersCollector(Collector):
    name = "google-careers"
    mode = "board"
    base = "https://www.google.com/about/careers/applications/jobs/results/"

    def __init__(self, pages: int = 8) -> None:
        self.pages = pages

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        discovered: dict[str, Posting] = {}
        scanned = 0
        for page in range(1, self.pages + 1):
            response = await client.get(
                self.base,
                params={"q": "intern 2027", "employment_type": "INTERN", "page": page},
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            page_ids: set[str] = set()
            for anchor in soup.find_all("a", href=True):
                href = str(anchor["href"])
                match = JOB_URL_RE.search(href)
                if not match:
                    continue
                source_id = match.group(1)
                page_ids.add(source_id)
                title = " ".join(anchor.get_text(" ", strip=True).split())
                if not title or title.lower() in {"learn more", "apply", "share"}:
                    slug = href.split("/results/", 1)[-1].split("?", 1)[0]
                    title = slug.split("-", 1)[-1].replace("-", " ").title()
                url = urljoin(self.base, href)
                candidate = classify(
                    Posting(
                        company="Google",
                        title=title,
                        apply_url=url,
                        source=self.name,
                        source_id=source_id,
                        locations=[],
                    )
                )
                if candidate.target_match != "not_internship":
                    discovered[source_id] = candidate
            scanned += len(page_ids)
            if page > 1 and not page_ids:
                break

        semaphore = asyncio.Semaphore(8)

        async def enrich(posting: Posting) -> Posting:
            async with semaphore:
                response = await client.get(posting.apply_url)
                response.raise_for_status()
                schema = json_ld_jobs(response.text)
                if schema:
                    parsed = posting_from_schema(schema[0], source=self.name)
                    if parsed:
                        parsed.company = "Google"
                        parsed.source_id = posting.source_id
                        return classify(parsed)
                soup = BeautifulSoup(response.text, "html.parser")
                heading = soup.find(["h1", "h2"])
                if heading:
                    posting.title = " ".join(heading.get_text(" ", strip=True).split())
                posting.description = " ".join(soup.get_text(" ", strip=True).split())[:20000]
                return classify(posting)

        postings = await asyncio.gather(*(enrich(item) for item in discovered.values()))
        return CollectorResult(
            source=self.name,
            postings=[item for item in postings if item.target_match != "not_internship"],
            complete=True,
            mode=self.mode,
            rows_scanned=scanned,
        )


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

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(f"https://api.lever.co/v0/postings/{self.site}", params={"mode": "json"})
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
                        locations=locations_from(job.get("location")) + locations_from(job.get("secondaryLocations")),
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


class WorkdayCollector(Collector):
    mode = "board"

    def __init__(self, company: str, host: str, tenant: str, site: str) -> None:
        self.company = company
        self.host = host.rstrip("/")
        self.tenant = tenant
        self.site = site
        self.name = f"workday:{tenant}:{site}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        endpoint = f"{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"
        postings: list[Posting] = []
        offset = 0
        total: int | None = None
        while total is None or offset < total:
            response = await client.post(
                endpoint,
                json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "intern"},
                headers={"Origin": self.host, "Referer": f"{self.host}/{self.site}"},
            )
            if response.status_code == 400 and offset == 0:
                response = await client.post(
                    endpoint,
                    json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
                    headers={"Origin": self.host, "Referer": f"{self.host}/{self.site}"},
                )
            response.raise_for_status()
            payload = response.json()
            total = int(payload.get("total") or len(payload.get("jobPostings", [])))
            page = payload.get("jobPostings", [])
            if not page:
                break
            for job in page:
                raw_posted = text(job.get("postedOn"))
                posted = None
                if match := re.search(r"(\d+)\s+Day", raw_posted, re.I):
                    posted = datetime.now(timezone.utc) - timedelta(days=int(match.group(1)))
                source_id = text(job.get("bulletFields", [""])[0] if job.get("bulletFields") else job.get("externalPath"))
                url = urljoin(f"{self.host}/{self.site}/", text(job.get("externalPath")))
                postings.append(
                    classify(
                        Posting(
                            company=self.company,
                            title=text(job.get("title")),
                            apply_url=url,
                            source=self.name,
                            source_id=source_id or url,
                            locations=locations_from(job.get("locationsText")),
                            employment_type=text(job.get("timeType")),
                            posted_at=posted,
                            posted_raw=raw_posted or None,
                            posted_precision="day" if posted else "unknown",
                            posted_confidence="approximate" if posted else "unknown",
                        )
                    )
                )
            offset += len(page)
            if len(page) < 20:
                break
        return CollectorResult(self.name, postings, True, self.mode, offset, total)


class SchemaPageCollector(Collector):
    mode = "verification"

    def __init__(self, company: str, urls: list[str], name: str | None = None) -> None:
        self.company = company
        self.urls = urls
        self.name = name or f"schema:{urlsplit(urls[0]).netloc}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        postings: list[Posting] = []
        for url in self.urls:
            response = await client.get(url)
            response.raise_for_status()
            for job in json_ld_jobs(response.text):
                parsed = posting_from_schema(job, source=self.name)
                if parsed:
                    parsed.company = self.company
                    postings.append(classify(parsed))
        return CollectorResult(self.name, postings, False, self.mode, len(self.urls))


class DatabricksIndexCollector(Collector):
    """Independent canary backstop while Databricks' employer page remains non-enumerable."""

    name = "index:databricks-linkedin"
    mode = "external-index"
    url = "https://www.linkedin.com/company/databricks/jobs"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(self.url, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        postings: list[Posting] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            title = " ".join(anchor.get_text(" ", strip=True).split())
            href = str(anchor["href"])
            if not re.search(r"\bintern\b", title, re.I) or "2027" not in title:
                continue
            source_id = re.sub(r"\D", "", href) or href
            if source_id in seen:
                continue
            seen.add(source_id)
            parent_text = " ".join((anchor.parent or anchor).get_text(" ", strip=True).split())
            postings.append(
                classify(
                    Posting(
                        company="Databricks",
                        title=title,
                        apply_url=urljoin(self.url, href),
                        source=self.name,
                        source_id=source_id,
                        source_mode="external-index",
                        description=parent_text,
                        posted_precision="unknown",
                    )
                )
            )
        return CollectorResult(self.name, postings, False, self.mode, len(seen))
