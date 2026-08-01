from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .classify import classify
from .collectors import Collector, json_ld_jobs, posting_from_schema, text
from .models import CollectorResult, Posting

_JOB_PATH = re.compile(r"/jobs/(\d+)(?:[-/]|$)", re.I)
_MAX_LISTING_PAGES = 50


class TeamtailorCollector(Collector):
    """Enumerate a Teamtailor board, including paginated listings.

    Teamtailor boards expose stable numeric job IDs in ordinary HTML links. Listing
    pagination and individual job failures are isolated so one withdrawn role does
    not erase the rest of an employer's current inventory.
    """

    mode = "board"

    def __init__(self, company: str, subdomain: str) -> None:
        self.company = company
        self.subdomain = subdomain.casefold().strip()
        self.name = f"teamtailor:{self.subdomain}"
        self.origin = f"https://{self.subdomain}.teamtailor.com"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        links: dict[str, str] = {}
        listing_failures = 0
        listing_pages = 0

        for page in range(1, _MAX_LISTING_PAGES + 1):
            listing_url = f"{self.origin}/jobs" if page == 1 else f"{self.origin}/jobs?page={page}"
            try:
                response = await client.get(listing_url)
                response.raise_for_status()
            except httpx.HTTPError:
                listing_failures += 1
                break

            listing_pages += 1
            before = len(links)
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                url = urljoin(str(response.url), str(anchor["href"]))
                parts = urlsplit(url)
                if parts.netloc.casefold() != f"{self.subdomain}.teamtailor.com":
                    continue
                match = _JOB_PATH.search(parts.path)
                if match:
                    links[match.group(1)] = url

            # Teamtailor pages repeat the last page for out-of-range page numbers.
            # Stop as soon as a page contributes no new stable job IDs.
            if len(links) == before:
                break

        semaphore = asyncio.Semaphore(12)

        async def fetch(source_id: str, url: str) -> Posting | None:
            async with semaphore:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError:
                    return None

                for schema in json_ld_jobs(response.text):
                    posting = posting_from_schema(schema, source=self.name)
                    if posting is not None:
                        posting.company = self.company
                        posting.source_id = source_id
                        posting.apply_url = str(response.url)
                        return classify(posting)

                soup = BeautifulSoup(response.text, "html.parser")
                heading = soup.find("h1")
                title = text(heading.get_text(" ", strip=True) if heading else "")
                if not title:
                    return None
                main = soup.find("main") or soup.body
                return classify(
                    Posting(
                        company=self.company,
                        title=title,
                        apply_url=str(response.url),
                        source=self.name,
                        source_id=source_id,
                        description=text(main.get_text(" ", strip=True) if main else ""),
                    )
                )

        results = await asyncio.gather(
            *(fetch(source_id, url) for source_id, url in sorted(links.items()))
        )
        postings = [posting for posting in results if posting is not None]
        detail_failures = len(links) - len(postings)
        complete = bool(links) and detail_failures == 0 and listing_failures == 0
        if not links:
            status = "unreachable" if listing_failures and listing_pages == 0 else "empty"
        elif detail_failures or listing_failures:
            status = "partial"
        else:
            status = "ok"

        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=complete,
            mode=self.mode,
            rows_scanned=len(links),
            expected_rows=len(links),
            status=status,
        )
