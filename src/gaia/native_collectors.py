from __future__ import annotations

import asyncio
import html
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .classify import classify
from .collectors import Collector, json_ld_jobs, posting_from_schema
from .models import CollectorResult, Posting

GOOGLE_JOB_RE = re.compile(
    r"/about/careers/applications/jobs/results/(\d+)(?:-([^?#\"'<>\\]+))?",
    re.I,
)
GENERIC_ANCHOR_TEXT = {"learn more", "apply", "share", "details"}


def _normalize_google_html(body: str) -> str:
    return html.unescape(body).replace("\\/", "/").replace("\\u0026", "&")


def _slug_title(slug: str | None) -> str:
    if not slug:
        return "Google internship"
    return re.sub(r"[-_]+", " ", slug).strip().title()


class GoogleInternshipCollector(Collector):
    """Enumerate Google's public internship search without hardcoded job IDs."""

    name = "google-careers"
    mode = "board-search"
    base = "https://www.google.com/about/careers/applications/jobs/results/"

    def __init__(self, pages: int = 10) -> None:
        self.pages = pages

    @staticmethod
    def _page_postings(body: str) -> dict[str, Posting]:
        normalized = _normalize_google_html(body)
        soup = BeautifulSoup(normalized, "html.parser")
        titles: dict[str, str] = {}
        hrefs: dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "")
            match = GOOGLE_JOB_RE.search(href)
            if not match:
                continue
            source_id = match.group(1)
            title = " ".join(anchor.get_text(" ", strip=True).split())
            if title.casefold() in GENERIC_ANCHOR_TEXT:
                title = ""
            titles[source_id] = title or titles.get(source_id) or _slug_title(match.group(2))
            hrefs[source_id] = href

        for match in GOOGLE_JOB_RE.finditer(normalized):
            source_id = match.group(1)
            titles.setdefault(source_id, _slug_title(match.group(2)))
            hrefs.setdefault(source_id, match.group(0))

        return {
            source_id: Posting(
                company="Google",
                title=titles[source_id],
                apply_url=urljoin(GoogleInternshipCollector.base, hrefs[source_id]),
                source=GoogleInternshipCollector.name,
                source_id=source_id,
            )
            for source_id in titles
        }

    async def _enrich(self, client: httpx.AsyncClient, posting: Posting) -> Posting:
        try:
            response = await client.get(posting.apply_url)
            response.raise_for_status()
        except httpx.HTTPError:
            return classify(posting)
        schemas = json_ld_jobs(response.text)
        if schemas:
            parsed = posting_from_schema(schemas[0], source=self.name)
            if parsed:
                parsed.company = "Google"
                parsed.source_id = posting.source_id
                return classify(parsed)
        soup = BeautifulSoup(response.text, "html.parser")
        heading = soup.find(["h1", "h2"])
        if heading:
            posting.title = " ".join(heading.get_text(" ", strip=True).split())
        posting.description = " ".join(soup.get_text(" ", strip=True).split())[:30000]
        return classify(posting)

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        discovered: dict[str, Posting] = {}
        exhausted = False
        for page in range(1, self.pages + 1):
            response = await client.get(
                self.base,
                params={"q": "intern", "sort_by": "date", "page": page},
            )
            response.raise_for_status()
            page_postings = self._page_postings(response.text)
            before = len(discovered)
            discovered.update(page_postings)
            if not page_postings or len(discovered) == before:
                exhausted = True
                break

        if not discovered:
            return CollectorResult(
                source=self.name,
                postings=[],
                complete=False,
                mode=self.mode,
                rows_scanned=0,
                status="broken",
                error="Google Careers search returned no parseable job identities",
                note="searched q=intern and inspected anchors plus embedded page data",
            )

        semaphore = asyncio.Semaphore(8)

        async def enrich(item: Posting) -> Posting:
            async with semaphore:
                return await self._enrich(client, item)

        enriched = await asyncio.gather(*(enrich(item) for item in discovered.values()))
        internships = [
            item
            for item in enriched
            if item.target_match != "not_internship"
            and ("intern" in item.title.casefold() or "intern" in item.employment_type.casefold())
        ]
        return CollectorResult(
            source=self.name,
            postings=internships,
            complete=exhausted,
            mode=self.mode,
            rows_scanned=len(discovered),
            expected_rows=len(discovered) if exhausted else None,
            status="ok" if exhausted else "truncated",
            note="Google Careers q=intern search, sorted by date",
        )
