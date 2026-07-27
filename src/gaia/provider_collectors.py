from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .classify import classify
from .collectors import (
    Collector,
    json_ld_jobs,
    locations_from,
    parse_date,
    posting_from_schema,
    text,
)
from .models import CollectorResult, Posting


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _flat_location(value: Any) -> list[str]:
    if isinstance(value, dict):
        joined = ", ".join(
            text(value.get(key))
            for key in ("city", "region", "state", "country")
            if text(value.get(key))
        )
        return [joined] if joined else locations_from(value)
    return locations_from(value)


class SmartRecruitersCollector(Collector):
    mode = "board"

    def __init__(self, company: str, identifier: str) -> None:
        self.company = company
        self.identifier = identifier
        self.name = f"smartrecruiters:{identifier}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        postings: list[Posting] = []
        offset = 0
        total: int | None = None
        while total is None or offset < total:
            response = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{self.identifier}/postings",
                params={"limit": 100, "offset": offset},
            )
            response.raise_for_status()
            payload = response.json()
            rows = list(payload.get("content") or payload.get("postings") or [])
            total = int(payload.get("totalFound") or payload.get("total") or len(rows))
            if not rows:
                break
            for job in rows:
                source_id = text(_first(job, "id", "uuid", "refNumber"))
                title = text(_first(job, "name", "title"))
                url = text(_first(job, "applyUrl", "referralUrl", "jobAdUrl"))
                if not url and source_id:
                    url = f"https://jobs.smartrecruiters.com/{self.identifier}/{source_id}"
                posted = parse_date(_first(job, "releasedDate", "createdOn", "createdAt"))
                postings.append(
                    classify(
                        Posting(
                            company=self.company,
                            title=title,
                            apply_url=url,
                            source=self.name,
                            source_id=source_id or url,
                            locations=_flat_location(job.get("location")),
                            employment_type=text(_first(job, "typeOfEmployment", "employmentType")),
                            posted_at=posted,
                            posted_raw=text(_first(job, "releasedDate", "createdOn", "createdAt")) or None,
                            posted_precision="timestamp" if posted else "unknown",
                            posted_confidence="official" if posted else "unknown",
                        )
                    )
                )
            offset += len(rows)
        complete = total is not None and offset >= total
        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=complete,
            mode=self.mode,
            rows_scanned=offset,
            expected_rows=total,
            status="ok" if complete else "truncated",
        )


class RecruiteeCollector(Collector):
    mode = "board"

    def __init__(self, company: str, subdomain: str) -> None:
        self.company = company
        self.subdomain = subdomain
        self.name = f"recruitee:{subdomain}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(f"https://{self.subdomain}.recruitee.com/api/offers/")
        response.raise_for_status()
        payload = response.json()
        rows = list(payload.get("offers") or payload.get("content") or payload.get("results") or [])
        postings: list[Posting] = []
        for job in rows:
            source_id = text(_first(job, "id", "slug", "offer_id"))
            url = text(_first(job, "careers_url", "url", "apply_url"))
            if not url and source_id:
                url = f"https://{self.subdomain}.recruitee.com/o/{source_id}"
            posted_raw = text(_first(job, "published_at", "publishedAt", "created_at"))
            posted = parse_date(posted_raw)
            location = _first(job, "location", "locations")
            if not location:
                location = {
                    "city": job.get("city"),
                    "region": job.get("state"),
                    "country": job.get("country"),
                }
            postings.append(
                classify(
                    Posting(
                        company=self.company,
                        title=text(_first(job, "title", "name")),
                        apply_url=url,
                        source=self.name,
                        source_id=source_id or url,
                        locations=_flat_location(location),
                        description=text(_first(job, "description", "description_plain")),
                        employment_type=text(_first(job, "employment_type", "contract_type")),
                        posted_at=posted,
                        posted_raw=posted_raw or None,
                        posted_precision="timestamp" if posted else "unknown",
                        posted_confidence="official" if posted else "unknown",
                    )
                )
            )
        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=True,
            mode=self.mode,
            rows_scanned=len(rows),
            expected_rows=len(rows),
            status="ok",
        )


class WorkableCollector(Collector):
    mode = "board"

    def __init__(self, company: str, subdomain: str) -> None:
        self.company = company
        self.subdomain = subdomain
        self.name = f"workable:{subdomain}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(f"https://www.workable.com/api/accounts/{self.subdomain}")
        response.raise_for_status()
        payload = response.json()
        rows = list(
            payload.get("jobs")
            or payload.get("results")
            or payload.get("content")
            or (payload.get("data") or {}).get("jobs")
            or []
        )
        postings: list[Posting] = []
        for job in rows:
            source_id = text(_first(job, "shortcode", "code", "id"))
            url = text(_first(job, "url", "application_url", "shortlink"))
            if not url and source_id:
                url = f"https://apply.workable.com/{self.subdomain}/j/{source_id}/"
            posted_raw = text(_first(job, "published_at", "created_at", "createdAt"))
            posted = parse_date(posted_raw)
            location = job.get("location") or {
                "city": job.get("city"),
                "region": job.get("state"),
                "country": job.get("country"),
            }
            postings.append(
                classify(
                    Posting(
                        company=self.company,
                        title=text(_first(job, "title", "name")),
                        apply_url=url,
                        source=self.name,
                        source_id=source_id or url,
                        locations=_flat_location(location),
                        description=text(_first(job, "description", "description_plain")),
                        employment_type=text(_first(job, "employment_type", "type")),
                        posted_at=posted,
                        posted_raw=posted_raw or None,
                        posted_precision="timestamp" if posted else "unknown",
                        posted_confidence="official" if posted else "unknown",
                    )
                )
            )
        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=True,
            mode=self.mode,
            rows_scanned=len(rows),
            expected_rows=len(rows),
            status="ok",
        )


def _detail_posting(html: str, url: str, *, company: str, source: str) -> Posting | None:
    schemas = json_ld_jobs(html)
    if schemas:
        posting = posting_from_schema(schemas[0], source=source)
        if posting is not None:
            return posting
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title_meta = soup.find("meta", property="og:title")
    title = text(heading.get_text(" ", strip=True) if heading else "")
    if not title and title_meta:
        title = text(title_meta.get("content"))
    if not title:
        return None
    date_node = soup.select_one('[itemprop="datePosted"], time[datetime]')
    posted_raw = text(
        date_node.get("content") or date_node.get("datetime") if date_node else ""
    )
    posted = parse_date(posted_raw)
    source_id_match = re.search(
        r"(?:/job/|career_job_req_id=)([A-Za-z0-9_-]+)", url, re.I
    )
    source_id = source_id_match.group(1) if source_id_match else url
    main = soup.find("main") or soup.find(attrs={"itemprop": "description"}) or soup.body
    return classify(
        Posting(
            company=company,
            title=title,
            apply_url=url,
            source=source,
            source_id=source_id,
            description=text(main.get_text(" ", strip=True) if main else ""),
            posted_at=posted,
            posted_raw=posted_raw or None,
            posted_precision="timestamp" if posted else "unknown",
            posted_confidence="official" if posted else "unknown",
        )
    )


async def _collect_detail_links(
    client: httpx.AsyncClient,
    *,
    listing_urls: list[str],
    link_pattern: re.Pattern[str],
    company: str,
    source: str,
) -> tuple[list[Posting], int]:
    links: set[str] = set()
    for listing_url in listing_urls:
        response = await client.get(listing_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            url = urljoin(str(response.url), str(anchor["href"]))
            if link_pattern.search(url):
                links.add(url)

    semaphore = asyncio.Semaphore(12)

    async def fetch(url: str) -> Posting | None:
        async with semaphore:
            response = await client.get(url)
            response.raise_for_status()
            return _detail_posting(
                response.text, str(response.url), company=company, source=source
            )

    postings = [
        posting
        for posting in await asyncio.gather(*(fetch(url) for url in sorted(links)))
        if posting is not None
    ]
    return postings, len(links)


class JobviteCollector(Collector):
    mode = "board"

    def __init__(self, company: str, slug: str) -> None:
        self.company = company
        self.slug = slug
        self.name = f"jobvite:{slug.casefold()}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        postings, scanned = await _collect_detail_links(
            client,
            listing_urls=[
                f"https://jobs.jobvite.com/{self.slug}/jobs",
                f"https://jobs.jobvite.com/{self.slug}/search?q=intern",
            ],
            link_pattern=re.compile(
                rf"jobs\.jobvite\.com/{re.escape(self.slug)}/job/[^/?#]+", re.I
            ),
            company=self.company,
            source=self.name,
        )
        return CollectorResult(
            self.name,
            postings,
            True,
            self.mode,
            scanned,
            scanned,
            status="ok" if postings else "empty",
        )


class ICIMSCollector(Collector):
    mode = "board-search"

    def __init__(self, company: str, host: str) -> None:
        self.company = company
        self.host = host.casefold()
        self.name = f"icims:{self.host}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        postings, scanned = await _collect_detail_links(
            client,
            listing_urls=[
                f"https://{self.host}/jobs/search?ss=1&searchKeyword=intern",
                f"https://{self.host}/jobs/search?ss=1&searchKeyword=co-op",
            ],
            link_pattern=re.compile(
                rf"{re.escape(self.host)}/jobs/\d+/[^?#]+/job", re.I
            ),
            company=self.company,
            source=self.name,
        )
        return CollectorResult(
            self.name,
            postings,
            True,
            self.mode,
            scanned,
            scanned,
            status="ok" if postings else "empty",
        )


class OracleCloudCollector(Collector):
    mode = "board-search"

    def __init__(self, company: str, origin: str, site: str) -> None:
        self.company = company
        self.origin = origin.rstrip("/")
        self.site = site
        self.name = f"oracle:{urlsplit(self.origin).netloc.casefold()}:{site.casefold()}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        postings, scanned = await _collect_detail_links(
            client,
            listing_urls=[
                f"{self.origin}/hcmUI/CandidateExperience/en/sites/{self.site}/requisitions"
                "?keyword=intern",
                f"{self.origin}/hcmUI/CandidateExperience/en/sites/{self.site}/requisitions"
                "?keyword=co-op",
            ],
            link_pattern=re.compile(
                rf"/hcmUI/CandidateExperience/[^/]+/sites/{re.escape(self.site)}/job/\d+",
                re.I,
            ),
            company=self.company,
            source=self.name,
        )
        return CollectorResult(
            self.name,
            postings,
            True,
            self.mode,
            scanned,
            scanned,
            status="ok" if postings else "empty",
        )


class SuccessFactorsCollector(Collector):
    mode = "board-search"

    def __init__(self, company: str, origin: str, company_id: str) -> None:
        self.company = company
        self.origin = origin.rstrip("/")
        self.company_id = company_id
        self.name = (
            f"successfactors:{urlsplit(self.origin).netloc.casefold()}:{company_id.casefold()}"
        )

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        query = (
            f"company={self.company_id}"
            "&career_ns=job_listing_summary&searchby=location&keywords=intern"
        )
        postings, scanned = await _collect_detail_links(
            client,
            listing_urls=[
                f"{self.origin}/career?{query}",
                f"{self.origin}/careers?{query}",
            ],
            link_pattern=re.compile(
                rf"{re.escape(urlsplit(self.origin).netloc)}.*career_job_req_id=\d+",
                re.I,
            ),
            company=self.company,
            source=self.name,
        )
        return CollectorResult(
            self.name,
            postings,
            True,
            self.mode,
            scanned,
            scanned,
            status="ok" if postings else "empty",
        )
