from __future__ import annotations

import asyncio
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from .classify import classify
from .collectors import Collector, json_ld_jobs, locations_from, posting_from_schema, text
from .models import CollectorResult, Posting
from .page_verification import page_is_closed, posting_from_unstructured_page

TARGET_MATCHES = {"exact", "year_confirmed", "source_confirmed"}
TECH_CATEGORIES = {"software", "ml-ai", "data", "security", "hardware", "quant", "product"}
WORKDAY_TERMS = (
    "intern",
    "internship",
    "co-op",
    "coop",
    "student",
    "university",
    "campus",
    "summer",
)
JOB_PATH_RE = re.compile(
    r"(?:^|/)(?:job|jobs|career|careers|position|positions|opening|openings|requisition)(?:/|$)",
    re.I,
)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]|$)")
SITEMAP_RE = re.compile(r"^\s*Sitemap:\s*(\S+)", re.I | re.M)


def _workday_relative(raw: str) -> tuple[datetime | None, str]:
    if re.search(r"\bToday\b", raw, re.I):
        return datetime.now(timezone.utc), "day"
    if match := re.search(r"(\d+)\+?\s+Day", raw, re.I):
        return datetime.now(timezone.utc) - timedelta(days=int(match.group(1))), "day"
    if ISO_DATE_RE.match(raw):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc), "timestamp"
        except ValueError:
            pass
    return None, "unknown"


class WorkdaySearchCollector(Collector):
    """Enumerate a high-recall internship search basis, not a company's entire board."""

    mode = "board-search"

    def __init__(
        self,
        company: str,
        host: str,
        tenant: str,
        site: str,
        *,
        terms: Iterable[str] = WORKDAY_TERMS,
    ) -> None:
        self.company = company
        self.host = host.rstrip("/")
        self.tenant = tenant
        self.site = site
        self.terms = tuple(dict.fromkeys(term.strip() for term in terms if term.strip()))
        self.name = f"workday:{tenant}:{site}"
        self.page_concurrency = max(1, int(os.getenv("GAIA_WORKDAY_PAGE_CONCURRENCY", "6")))
        self.detail_concurrency = max(1, int(os.getenv("GAIA_DETAIL_CONCURRENCY", "8")))
        self.max_per_term = max(20, int(os.getenv("GAIA_WORKDAY_MAX_PER_TERM", "4000")))

    @property
    def endpoint(self) -> str:
        return f"{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    async def _page(
        self,
        client: httpx.AsyncClient,
        term: str,
        offset: int,
    ) -> dict[str, object]:
        response = await client.post(
            self.endpoint,
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term},
            headers={"Origin": self.host, "Referer": f"{self.host}/{self.site}"},
        )
        response.raise_for_status()
        return response.json()

    async def _query(
        self,
        client: httpx.AsyncClient,
        term: str,
    ) -> tuple[list[dict[str, object]], bool, int]:
        first = await self._page(client, term, 0)
        total = int(first.get("total") or len(first.get("jobPostings", [])))
        capped_total = min(total, self.max_per_term)
        pages = list(first.get("jobPostings", []))
        offsets = list(range(20, capped_total, 20))
        semaphore = asyncio.Semaphore(self.page_concurrency)

        async def fetch(offset: int) -> list[dict[str, object]]:
            async with semaphore:
                payload = await self._page(client, term, offset)
                return list(payload.get("jobPostings", []))

        batch_size = self.page_concurrency * 4
        for start in range(0, len(offsets), batch_size):
            batch = offsets[start : start + batch_size]
            for page in await asyncio.gather(*(fetch(offset) for offset in batch)):
                pages.extend(page)
        complete = total <= self.max_per_term and len(pages) >= total
        return pages, complete, total

    def _posting(self, job: dict[str, object]) -> Posting:
        raw_posted = text(job.get("postedOn"))
        posted_at, precision = _workday_relative(raw_posted)
        external_path = text(job.get("externalPath"))
        bullet_fields = job.get("bulletFields") or []
        source_id = text(bullet_fields[0] if bullet_fields else external_path)
        url = f"{self.host}/{self.site}/{external_path.lstrip('/')}"
        return classify(
            Posting(
                company=self.company,
                title=text(job.get("title")),
                apply_url=url,
                source=self.name,
                source_id=source_id or url,
                locations=locations_from(job.get("locationsText")),
                employment_type=text(job.get("timeType")),
                posted_at=posted_at,
                posted_raw=raw_posted or None,
                posted_precision=precision,
                posted_confidence="approximate" if posted_at else "unknown",
            )
        )

    async def _enrich(self, client: httpx.AsyncClient, posting: Posting) -> Posting:
        parts = urlsplit(posting.apply_url)
        marker = f"/{self.site}/"
        if marker not in parts.path:
            return posting
        external_path = "/" + parts.path.split(marker, 1)[1].lstrip("/")
        detail_url = f"{self.host}/wday/cxs/{self.tenant}/{self.site}{external_path}"
        try:
            response = await client.get(
                detail_url,
                headers={"Referer": posting.apply_url, "Accept": "application/json"},
            )
            response.raise_for_status()
            info = response.json().get("jobPostingInfo") or {}
        except (httpx.HTTPError, ValueError, TypeError):
            return posting

        posting.description = text(info.get("jobDescription")) or posting.description
        posting.employment_type = text(info.get("timeType")) or posting.employment_type
        extra_locations = locations_from(info.get("additionalLocations"))
        if location := text(info.get("location")):
            extra_locations.append(location)
        if extra_locations:
            posting.locations = sorted(set([*posting.locations, *extra_locations]))
        if req_id := text(info.get("jobReqId")):
            posting.source_id = req_id
        raw_posted = text(info.get("postedOn"))
        if raw_posted:
            posted_at, precision = _workday_relative(raw_posted)
            posting.posted_raw = raw_posted
            if posted_at:
                posting.posted_at = posted_at
                posting.posted_precision = precision
                posting.posted_confidence = (
                    "official" if precision == "timestamp" else "approximate"
                )
        return classify(posting)

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        by_path: dict[str, dict[str, object]] = {}
        complete = True
        totals = 0
        for term in self.terms:
            rows, query_complete, total = await self._query(client, term)
            complete = complete and query_complete
            totals += total
            for row in rows:
                key = text(row.get("externalPath")) or text(row.get("title"))
                if key:
                    by_path[key] = row

        postings = [self._posting(row) for row in by_path.values()]
        enrichable = [
            item
            for item in postings
            if item.target_match in TARGET_MATCHES or item.category in TECH_CATEGORIES
        ]
        semaphore = asyncio.Semaphore(self.detail_concurrency)

        async def enrich(item: Posting) -> Posting:
            async with semaphore:
                return await self._enrich(client, item)

        enriched = await asyncio.gather(*(enrich(item) for item in enrichable))
        enriched_by_url = {item.canonical_apply_url: item for item in enriched}
        postings = [enriched_by_url.get(item.canonical_apply_url, item) for item in postings]
        note = f"query-scoped board search: {', '.join(self.terms)}"
        if not complete:
            note += f"; one or more queries exceeded {self.max_per_term:,} results"
        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=complete,
            mode=self.mode,
            rows_scanned=len(by_path),
            expected_rows=len(by_path) if complete else totals,
            status="ok" if complete else "truncated",
            note=note,
        )


def _xml_locations(body: str) -> tuple[list[str], bool]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [], False
    tag = root.tag.rsplit("}", 1)[-1].lower()
    locations = [
        (node.text or "").strip()
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1].lower() == "loc" and (node.text or "").strip()
    ]
    return locations, tag == "sitemapindex"


class SitemapDomainCollector(Collector):
    """Enumerate a custom employer domain when it exposes sitemaps."""

    mode = "domain"

    def __init__(self, company: str, host: str, seed_urls: list[str]) -> None:
        self.company = company
        self.host = host.lower()
        self.seed_urls = sorted(set(seed_urls))
        self.name = f"domain:{self.host}:{company}"
        self.max_urls = max(50, int(os.getenv("GAIA_DOMAIN_MAX_URLS", "500")))
        self.fetch_concurrency = max(1, int(os.getenv("GAIA_DOMAIN_CONCURRENCY", "12")))

    async def _discover_sitemaps(self, client: httpx.AsyncClient) -> list[str]:
        candidates: list[str] = []
        for root in (f"https://{self.host}", f"http://{self.host}"):
            try:
                response = await client.get(f"{root}/robots.txt")
                if response.status_code < 400:
                    candidates.extend(SITEMAP_RE.findall(response.text))
                    break
            except httpx.HTTPError:
                continue
        candidates.extend(
            f"https://{self.host}{path}"
            for path in (
                "/sitemap.xml",
                "/sitemap_index.xml",
                "/jobs-sitemap.xml",
                "/careers-sitemap.xml",
            )
        )
        return list(dict.fromkeys(candidates))

    async def _candidate_urls(self, client: httpx.AsyncClient) -> tuple[list[str], bool]:
        queue = await self._discover_sitemaps(client)
        seen_sitemaps: set[str] = set()
        urls: set[str] = set(self.seed_urls)
        complete = True
        while queue and len(seen_sitemaps) < 24:
            sitemap = queue.pop(0)
            if sitemap in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap)
            try:
                response = await client.get(sitemap)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            locations, is_index = _xml_locations(response.text)
            if is_index:
                queue.extend(location for location in locations if location not in seen_sitemaps)
                continue
            for location in locations:
                parts = urlsplit(location)
                if parts.netloc.lower() != self.host:
                    continue
                if JOB_PATH_RE.search(parts.path):
                    urls.add(location)
                    if len(urls) >= self.max_urls:
                        return sorted(urls), False
        return sorted(urls), complete

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        urls, enumerated_all = await self._candidate_urls(client)
        semaphore = asyncio.Semaphore(self.fetch_concurrency)
        postings: list[Posting] = []
        blocked = stale = unstructured = 0

        async def fetch(url: str) -> list[Posting]:
            nonlocal blocked, stale, unstructured
            async with semaphore:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {401, 403, 429}:
                        blocked += 1
                    elif exc.response.status_code in {404, 410}:
                        stale += 1
                    return []
                except httpx.HTTPError:
                    return []
                if page_is_closed(response.text):
                    stale += 1
                    return []
                jobs = json_ld_jobs(response.text)
                if not jobs:
                    fallback = posting_from_unstructured_page(
                        response.text,
                        page_url=str(response.url),
                        company=self.company,
                        source=self.name,
                    )
                    if fallback:
                        return [fallback]
                    unstructured += 1
                    return []
                output: list[Posting] = []
                for job in jobs:
                    parsed = posting_from_schema(
                        job,
                        source=self.name,
                        source_mode="verification",
                    )
                    if parsed:
                        parsed.company = self.company
                        output.append(classify(parsed))
                return output

        batch_size = self.fetch_concurrency * 4
        for start in range(0, len(urls), batch_size):
            batch = urls[start : start + batch_size]
            for result in await asyncio.gather(*(fetch(url) for url in batch)):
                postings.extend(result)

        discovered = {item.canonical_apply_url: item for item in postings}
        notes = [f"{len(urls)} candidate pages"]
        if blocked:
            notes.append(f"{blocked} blocked")
        if stale:
            notes.append(f"{stale} stale")
        if unstructured:
            notes.append(f"{unstructured} without sufficient job evidence")
        complete = enumerated_all and bool(urls) and len(urls) > len(self.seed_urls)
        status = "ok" if complete else ("verified" if discovered else "unstructured")
        return CollectorResult(
            source=self.name,
            postings=list(discovered.values()),
            complete=complete,
            mode=self.mode,
            rows_scanned=len(urls),
            expected_rows=len(urls) if complete else None,
            status=status,
            note="; ".join(notes),
        )
