from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .classify import classify
from .collectors import Collector, json_ld_jobs, posting_from_schema, text
from .models import CollectorResult, Posting

_JOB_PATH = re.compile(r"/(?:[a-z]{2}(?:-[A-Z]{2})?/)?[^/]+/jobs/([0-9a-f-]{36})(?:/|$)", re.I)


class RipplingCollector(Collector):
    """Enumerate a public Rippling ATS board and isolate per-job failures.

    Rippling boards are rendered as ordinary HTML and expose stable UUID job links.
    The board has appeared both with and without a locale prefix, so both forms are
    probed. A broken or withdrawn detail page must not discard every other opening.
    """

    mode = "board"

    def __init__(self, company: str, slug: str) -> None:
        self.company = company
        self.slug = slug.casefold().strip()
        self.name = f"rippling:{self.slug}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        links: dict[str, str] = {}
        listing_failures = 0
        for listing_url in (
            f"https://ats.rippling.com/{self.slug}/jobs",
            f"https://ats.rippling.com/en-US/{self.slug}/jobs",
        ):
            try:
                response = await client.get(listing_url)
                response.raise_for_status()
            except httpx.HTTPError:
                listing_failures += 1
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                url = urljoin(str(response.url), str(anchor["href"]))
                match = _JOB_PATH.search(urlsplit(url).path)
                if match and "/apply" not in urlsplit(url).path.casefold():
                    links[match.group(1).casefold()] = url

        semaphore = asyncio.Semaphore(12)

        async def fetch(source_id: str, url: str) -> Posting | None:
            async with semaphore:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError:
                    return None
                schemas = json_ld_jobs(response.text)
                for schema in schemas:
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
        complete = bool(links) and detail_failures == 0
        if not links:
            status = "unreachable" if listing_failures == 2 else "empty"
        elif detail_failures:
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
