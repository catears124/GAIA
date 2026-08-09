from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

import httpx

from .classify import INTERN_RE, PROGRAM_RE, classify
from .collectors import Collector, json_ld_jobs, parse_date, posting_from_schema, text
from .models import CollectorResult, Posting

ISOLVED_DOMAIN_ID_RE = re.compile(
    r"courierCurrentRouteData\s*=\s*\{[^{}]*?[\"']domain_id[\"']\s*:\s*[\"']?(\d{1,12})[\"']?",
    re.I,
)
JAZZ_HREF_RE = re.compile(
    r'href=["\'](https?://([^/"\']+\.applytojob\.com)/apply/([A-Za-z0-9]{4,})(?:/[^"\']*)?)["\']',
    re.I,
)
YEAR_2027_RE = re.compile(r"\b2027\b")
JAZZ_DETAIL_LIMIT = 80


def humanize_slug(slug: str) -> str:
    value = re.sub(r"[-_]+", " ", slug).strip()
    return " ".join(part.capitalize() for part in value.split()) or slug


def extract_isolved_domain_id(html: str) -> str | None:
    match = ISOLVED_DOMAIN_ID_RE.search(html)
    return match.group(1) if match else None


def _isolved_location(item: dict[str, object]) -> list[str]:
    city = text(item.get("city"))
    region = text(item.get("abbreviation") or item.get("stateName"))
    if city and region:
        return [f"{city}, {region}"]
    if city:
        return [city]
    if region:
        return [region]
    fallback = text(item.get("jobLocation"))
    return [fallback] if fallback else []


def parse_isolved_jobs(
    payload: object,
    *,
    slug: str,
    company: str,
    source: str,
) -> list[Posting]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("isolvedhire job response did not report success")
    data = payload.get("data")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("isolvedhire job response is missing data.jobs")

    postings: list[Posting] = []
    for raw in jobs:
        if not isinstance(raw, dict):
            continue
        source_id = text(raw.get("id"))
        title = text(raw.get("title"))
        if not source_id or not title:
            continue
        raw_url = text(raw.get("jobUrl"))
        try:
            host = urlsplit(raw_url).hostname or ""
        except ValueError:
            host = ""
        apply_url = (
            raw_url
            if raw_url.startswith("https://") and host.endswith(".isolvedhire.com")
            else f"https://{slug}.isolvedhire.com/jobs/{source_id}"
        )
        posted = parse_date(raw.get("startDateRef"))
        employment_type = text(raw.get("employmentType"))
        workplace = text(raw.get("workplaceType"))
        description = " ".join(
            value
            for value in (
                text(raw.get("classification")),
                text(raw.get("jobCategory")),
                workplace,
            )
            if value
        )
        postings.append(
            classify(
                Posting(
                    company=company,
                    title=title,
                    apply_url=apply_url,
                    source=source,
                    source_id=source_id,
                    locations=_isolved_location(raw),
                    source_mode="direct",
                    description=description,
                    employment_type=employment_type,
                    posted_at=posted,
                    posted_raw=text(raw.get("startDateRef")) or None,
                    posted_precision="date" if posted else "unknown",
                    posted_confidence="official" if posted else "unknown",
                )
            )
        )
    return postings


class ISolvedHireCollector(Collector):
    mode = "board"

    def __init__(self, slug: str, company: str | None = None) -> None:
        self.slug = slug.casefold().strip()
        self.company = company or humanize_slug(self.slug)
        self.name = f"isolvedhire:{self.slug}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        board_url = f"https://{self.slug}.isolvedhire.com/jobs/"
        response = await client.get(board_url)
        response.raise_for_status()
        domain_id = extract_isolved_domain_id(response.text)
        if not domain_id:
            return CollectorResult(
                self.name,
                [],
                False,
                self.mode,
                0,
                status="broken",
                note="isolvedhire board missing domain_id bootstrap",
            )
        api_url = f"https://{self.slug}.isolvedhire.com/core/jobs/{domain_id}"
        response = await client.get(api_url, params={"getParams": '{"isInternal":0}'})
        response.raise_for_status()
        try:
            postings = parse_isolved_jobs(
                response.json(), slug=self.slug, company=self.company, source=self.name
            )
        except ValueError as exc:
            return CollectorResult(
                self.name,
                [],
                False,
                self.mode,
                0,
                status="broken",
                error=str(exc),
            )
        return CollectorResult(
            self.name,
            postings,
            True,
            self.mode,
            len(postings),
            len(postings),
            status="loaded" if postings else "empty",
        )


def parse_jazz_target_links(html: str, *, slug: str) -> list[tuple[str, str]]:
    expected_host = f"{slug}.applytojob.com".casefold()
    found: dict[str, str] = {}
    for match in JAZZ_HREF_RE.finditer(html):
        raw_url, host, source_id = match.group(1), match.group(2), match.group(3)
        if host.casefold() != expected_host:
            continue
        # The board's human-readable title slug is enough for a cheap first-stage
        # internship filter. The detail JSON-LD is still authoritative and GAIA's
        # classifier decides whether the role is actually a 2027 target.
        path_text = urlsplit(raw_url).path.replace("-", " ").replace("_", " ")
        if not (INTERN_RE.search(path_text) or PROGRAM_RE.search(path_text)):
            continue
        canonical = raw_url.split("?", 1)[0].split("#", 1)[0]
        found[source_id] = canonical
    return list(found.items())[:JAZZ_DETAIL_LIMIT]


class JazzHRTargetCollector(Collector):
    """Target-only JazzHR collector optimized for GAIA's internship corpus.

    JazzHR has no cheap all-jobs JSON endpoint. We therefore fetch one tenant board,
    use its human-readable apply-link slugs to select internship-like roles, then read
    only those detail pages' JobPosting JSON-LD. This avoids the pathological
    board-plus-every-job fan-out of a general-purpose JazzHR scraper.
    """

    mode = "target-board"

    def __init__(self, slug: str, company: str | None = None, detail_concurrency: int = 4) -> None:
        self.slug = slug.casefold().strip()
        self.company = company or humanize_slug(self.slug)
        self.detail_concurrency = max(1, min(detail_concurrency, 8))
        self.name = f"jazzhr:{self.slug}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        board_url = f"https://{self.slug}.applytojob.com/apply/"
        response = await client.get(board_url)
        response.raise_for_status()
        candidates = parse_jazz_target_links(response.text, slug=self.slug)
        semaphore = asyncio.Semaphore(self.detail_concurrency)
        failures = 0

        async def fetch(source_id: str, url: str) -> Posting | None:
            nonlocal failures
            async with semaphore:
                try:
                    detail = await client.get(url)
                    detail.raise_for_status()
                    jobs = json_ld_jobs(detail.text)
                    if not jobs:
                        failures += 1
                        return None
                    schema = jobs[0]
                    posting = posting_from_schema(schema, source=self.name, source_mode="direct")
                    if posting is None:
                        failures += 1
                        return None
                    posting.source_id = source_id
                    if not posting.company or posting.company == self.name:
                        posting.company = self.company
                    return posting
                except (httpx.HTTPError, ValueError):
                    failures += 1
                    return None

        rows = await asyncio.gather(*(fetch(source_id, url) for source_id, url in candidates))
        postings = [posting for posting in rows if posting is not None]
        complete = not candidates or failures <= max(1, len(candidates) // 2)
        return CollectorResult(
            self.name,
            postings,
            complete,
            self.mode,
            len(candidates),
            len(candidates),
            status="loaded" if complete else "broken",
            error=None if complete else f"{failures}/{len(candidates)} target detail pages failed",
        )
