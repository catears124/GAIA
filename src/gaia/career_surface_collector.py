from __future__ import annotations

import asyncio
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .classify import classify
from .collectors import json_ld_jobs, posting_from_schema
from .market_collectors import SitemapDomainCollector
from .models import CollectorResult, Posting, canonical_url
from .page_verification import page_is_closed, posting_from_unstructured_page

CAREER_PATHS = (
    "/careers",
    "/careers/jobs",
    "/jobs",
    "/join-us",
    "/join_us",
    "/work-with-us",
    "/open-positions",
    "/open-roles",
    "/about/careers",
)
CAREER_MARKERS = (
    "career",
    "careers",
    "job",
    "jobs",
    "join-us",
    "join_us",
    "work-with-us",
    "open-position",
    "open-role",
    "opportunit",
    "vacanc",
    "requisition",
)
DETAIL_MARKERS = (
    "/job/",
    "/jobs/",
    "/position/",
    "/positions/",
    "/opening/",
    "/openings/",
    "/requisition/",
    "/requisitions/",
    "/apply/",
    "/details/",
)
PROVIDER_HOST_FRAGMENTS = {
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "myworkdayjobs.com": "workday",
    "smartrecruiters.com": "smartrecruiters",
    "recruitee.com": "recruitee",
    "workable.com": "workable",
    "jobvite.com": "jobvite",
    "icims.com": "icims",
    "oraclecloud.com": "oracle-cloud",
    "successfactors.": "successfactors",
    "rippling.com": "rippling",
    "teamtailor.com": "teamtailor",
    "bamboohr.com": "bamboohr",
    "dayforcehcm.com": "dayforce",
    "eightfold.ai": "eightfold",
    "phenompeople.com": "phenom",
    "phenom.com": "phenom",
    "ukg.com": "ukg",
}
SITEMAP_RE = re.compile(r"^\s*Sitemap:\s*(\S+)", re.I | re.M)
URL_RE = re.compile(r"https?(?::|%3A)(?:\\?/|%2F){2}[^\s\"'<>\\]+", re.I)
JOB_ID_RE = re.compile(
    r"(?:/|[?&])(?:job|jobs|jobid|job_id|gh_jid|req|requisition)[=/_-]?[0-9a-f-]{3,}",
    re.I,
)
MULTIPART_PUBLIC_SUFFIXES = {
    "co.uk",
    "com.au",
    "com.br",
    "com.mx",
    "co.jp",
    "co.in",
    "co.nz",
    "com.sg",
}


@dataclass(slots=True)
class _Enumeration:
    pages: set[str] = field(default_factory=set)
    provider_urls: set[str] = field(default_factory=set)
    cached_html: dict[str, str] = field(default_factory=dict)
    sitemap_documents: int = 0
    career_sitemap_urls: int = 0
    feed_documents: int = 0
    landing_pages: int = 0
    reachable_landings: int = 0
    reachable_career_landings: int = 0
    blocked: int = 0
    stale: int = 0
    hard_errors: int = 0
    truncated: bool = False


def provider_kind(url: str) -> str | None:
    host = urlsplit(url).netloc.casefold().split(":", 1)[0]
    for fragment, kind in PROVIDER_HOST_FRAGMENTS.items():
        if fragment.endswith("."):
            if fragment in host:
                return kind
        elif host == fragment or host.endswith(f".{fragment}"):
            return kind
    return None


def _origin(host: str) -> str:
    return f"https://{host.casefold().strip().strip('/')}"


def _registrable_domain(host: str) -> str:
    labels = host.casefold().split(".")
    if len(labels) < 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in MULTIPART_PUBLIC_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix


def _normalized_http_url(url: str) -> str | None:
    raw = url.strip().replace("\\/", "/")
    raw = re.sub("%3A", ":", raw, flags=re.I)
    raw = re.sub("%2F", "/", raw, flags=re.I)
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _same_host(url: str, host: str) -> bool:
    return urlsplit(url).netloc.casefold().split(":", 1)[0] == host.casefold()


def _careerish(url: str, label: str = "") -> bool:
    parts = urlsplit(url)
    haystack = f"{parts.path} {parts.query} {label}".casefold()
    return any(marker in haystack for marker in CAREER_MARKERS)


def _detailish(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.casefold()
    return any(marker in path for marker in DETAIL_MARKERS) and (
        bool(JOB_ID_RE.search(url))
        or len([segment for segment in parts.path.split("/") if segment]) >= 3
    )


def career_seed_urls(host: str, seed_urls: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Generate bounded, independently useful career entry points for an employer host."""

    host = host.casefold().split(":", 1)[0].strip()
    if not host:
        return []
    output: list[str] = []
    for raw in seed_urls:
        normalized = _normalized_http_url(str(raw))
        if normalized:
            output.append(normalized)
    output.append(_origin(host))

    # Shared ATS hosts are tenant/path scoped. Guessing root-level career paths on them
    # creates cross-employer noise, so retain only observed tenant URLs and the origin.
    if provider_kind(_origin(host)) is None:
        output.extend(f"{_origin(host)}{path}" for path in CAREER_PATHS)
        if not host.startswith(("jobs.", "careers.")):
            base = _registrable_domain(host)
            output.extend((f"https://jobs.{base}/", f"https://careers.{base}/"))
    return list(dict.fromkeys(output))


def _xml_locations(body: str) -> tuple[list[str], bool]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [], False
    tag = root.tag.rsplit("}", 1)[-1].casefold()
    locations = [
        (node.text or "").strip()
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1].casefold() == "loc" and (node.text or "").strip()
    ]
    return locations, tag == "sitemapindex"


def _xml_document_links(body: str, base_url: str) -> tuple[list[tuple[str, str]], bool]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [], False
    root_tag = root.tag.rsplit("}", 1)[-1].casefold()
    is_feed = root_tag in {"rss", "feed", "rdf"}
    output: list[tuple[str, str]] = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].casefold()
        raw = ""
        if tag == "link":
            raw = str(node.attrib.get("href") or node.text or "").strip()
        elif tag in {"guid", "loc"}:
            raw = str(node.text or "").strip()
        if not raw:
            continue
        normalized = _normalized_http_url(urljoin(base_url, raw))
        if normalized:
            output.append((normalized, tag))
    return list(dict.fromkeys(output)), is_feed


def _embedded_urls(body: str) -> list[str]:
    output: list[str] = []
    for match in URL_RE.findall(body):
        normalized = _normalized_http_url(match.rstrip("),.;"))
        if normalized:
            output.append(normalized)
    return list(dict.fromkeys(output))


def _links(body: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(body, "html.parser")
    output: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        normalized = _normalized_http_url(urljoin(base_url, str(anchor.get("href"))))
        if normalized:
            output.append((normalized, anchor.get_text(" ", strip=True)))
    for link in soup.find_all("link", href=True):
        label = " ".join(
            [
                " ".join(str(value) for value in (link.get("rel") or [])),
                str(link.get("type") or ""),
                str(link.get("title") or ""),
            ]
        ).strip()
        normalized = _normalized_http_url(urljoin(base_url, str(link.get("href"))))
        if normalized and (
            "alternate" in label.casefold()
            or "rss" in label.casefold()
            or "atom" in label.casefold()
            or _careerish(normalized, label)
        ):
            output.append((normalized, label))
    output.extend((url, "") for url in _embedded_urls(body))
    unique: dict[str, str] = {}
    for url, label in output:
        unique[url] = unique.get(url) or label
    return list(unique.items())


def _document_links(body: str, base_url: str) -> tuple[list[tuple[str, str]], bool]:
    links = _links(body, base_url)
    xml_links, is_feed = _xml_document_links(body, base_url)
    unique = {url: label for url, label in links}
    for url, label in xml_links:
        unique[url] = unique.get(url) or label
    return list(unique.items()), is_feed


class CareerSurfaceCollector(SitemapDomainCollector):
    """Recursively enumerate an employer's own career graph.

    The collector combines robots/sitemaps, common career paths, bounded link
    recursion, embedded ATS URLs, RSS/Atom feeds, Schema.org JobPosting extraction,
    and guarded unstructured detail parsing. A complete empty board is reported only
    after a reachable career-specific surface was exhaustively traversed.
    """

    mode = "board-search"

    def __init__(self, company: str, host: str, seed_urls: list[str]) -> None:
        super().__init__(company, host, career_seed_urls(host, seed_urls))
        self.max_pages = max(24, int(os.getenv("GAIA_CAREER_MAX_PAGES", "240")))
        self.max_sitemaps = max(4, int(os.getenv("GAIA_CAREER_MAX_SITEMAPS", "32")))
        self.max_depth = max(1, int(os.getenv("GAIA_CAREER_MAX_DEPTH", "2")))
        self.fetch_concurrency = max(1, int(os.getenv("GAIA_CAREER_CONCURRENCY", "12")))

    async def _enumerate_sitemaps(
        self,
        client: httpx.AsyncClient,
        result: _Enumeration,
    ) -> None:
        sitemap_queue: list[str] = []
        for root in (_origin(self.host), f"http://{self.host}"):
            try:
                response = await client.get(f"{root}/robots.txt")
            except httpx.HTTPError:
                continue
            if response.status_code < 400:
                sitemap_queue.extend(SITEMAP_RE.findall(response.text))
                break
        sitemap_queue.extend(
            f"{_origin(self.host)}{path}"
            for path in (
                "/sitemap.xml",
                "/sitemap_index.xml",
                "/sitemap-index.xml",
                "/jobs-sitemap.xml",
                "/careers-sitemap.xml",
            )
        )

        seen: set[str] = set()
        while sitemap_queue and len(seen) < self.max_sitemaps:
            sitemap = sitemap_queue.pop(0)
            normalized = _normalized_http_url(sitemap)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            try:
                response = await client.get(normalized)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            locations, is_index = _xml_locations(response.text)
            if not locations:
                continue
            result.sitemap_documents += 1
            if is_index:
                sitemap_queue.extend(location for location in locations if location not in seen)
                continue
            for location in locations:
                normalized_location = _normalized_http_url(location)
                if not normalized_location:
                    continue
                if provider_kind(normalized_location):
                    result.provider_urls.add(normalized_location)
                    result.career_sitemap_urls += 1
                elif _same_host(normalized_location, self.host) and _careerish(normalized_location):
                    if normalized_location not in result.pages:
                        result.career_sitemap_urls += 1
                    result.pages.add(normalized_location)
                if len(result.pages) >= self.max_pages:
                    result.truncated = True
                    return
        if sitemap_queue:
            result.truncated = True

    async def _enumerate_landings(
        self,
        client: httpx.AsyncClient,
        result: _Enumeration,
    ) -> None:
        queue: list[tuple[str, int]] = [(url, 0) for url in self.seed_urls]
        seen: set[str] = set()
        semaphore = asyncio.Semaphore(self.fetch_concurrency)

        async def fetch(url: str, depth: int) -> tuple[str, int, httpx.Response | Exception]:
            async with semaphore:
                try:
                    return url, depth, await client.get(url)
                except Exception as exc:  # isolated transport failure
                    return url, depth, exc

        while queue and len(seen) < self.max_pages:
            batch: list[tuple[str, int]] = []
            while queue and len(batch) < self.fetch_concurrency:
                url, depth = queue.pop(0)
                key = canonical_url(url)
                if key in seen:
                    continue
                seen.add(key)
                batch.append((url, depth))
            if not batch:
                continue
            outcomes = await asyncio.gather(*(fetch(url, depth) for url, depth in batch))
            for _url, depth, outcome in outcomes:
                result.landing_pages += 1
                if isinstance(outcome, Exception):
                    continue
                response = outcome
                if response.status_code in {401, 403, 429}:
                    result.blocked += 1
                    continue
                if response.status_code in {404, 410}:
                    result.stale += 1
                    continue
                if response.status_code >= 500:
                    result.hard_errors += 1
                    continue
                if response.status_code >= 400:
                    continue
                content_type = response.headers.get("content-type", "").casefold()
                allowed_types = ("html", "text", "xml", "rss", "atom", "json")
                if content_type and not any(value in content_type for value in allowed_types):
                    continue
                final_url = str(response.url)
                links, is_feed = _document_links(response.text, final_url)
                schemas = json_ld_jobs(response.text)
                result.reachable_landings += 1
                if is_feed:
                    result.feed_documents += 1
                if (
                    _careerish(final_url)
                    or schemas
                    or is_feed
                    or any(provider_kind(link) or _careerish(link, label) for link, label in links)
                ):
                    result.reachable_career_landings += 1
                result.cached_html[canonical_url(final_url)] = response.text
                if schemas or _detailish(final_url):
                    result.pages.add(final_url)
                for link, label in links:
                    kind = provider_kind(link)
                    if kind:
                        result.provider_urls.add(link)
                        continue
                    if not _same_host(link, self.host):
                        continue
                    if _detailish(link):
                        result.pages.add(link)
                    elif depth < self.max_depth and _careerish(link, label):
                        queue.append((link, depth + 1))
                    if len(result.pages) >= self.max_pages:
                        result.truncated = True
                        return
        if queue:
            result.truncated = True

    async def _enumerate(self, client: httpx.AsyncClient) -> _Enumeration:
        result = _Enumeration()
        await asyncio.gather(
            self._enumerate_sitemaps(client, result),
            self._enumerate_landings(client, result),
        )
        return result

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        enumeration = await self._enumerate(client)
        semaphore = asyncio.Semaphore(self.fetch_concurrency)
        blocked = enumeration.blocked
        stale = enumeration.stale
        hard_errors = enumeration.hard_errors
        unstructured = 0

        async def parse(url: str) -> list[Posting]:
            nonlocal blocked, stale, hard_errors, unstructured
            cached = enumeration.cached_html.get(canonical_url(url))
            final_url = url
            body = cached
            if body is None:
                async with semaphore:
                    try:
                        response = await client.get(url)
                    except httpx.HTTPError:
                        hard_errors += 1
                        return []
                if response.status_code in {401, 403, 429}:
                    blocked += 1
                    return []
                if response.status_code in {404, 410}:
                    stale += 1
                    return []
                if response.status_code >= 400:
                    hard_errors += 1
                    return []
                final_url = str(response.url)
                body = response.text
            if page_is_closed(body):
                stale += 1
                return []

            output: list[Posting] = []
            for schema in json_ld_jobs(body):
                posting = posting_from_schema(schema, source=self.name)
                if posting is None:
                    continue
                posting.company = self.company
                output.append(classify(posting))
            if output:
                return output

            if _detailish(final_url):
                fallback = posting_from_unstructured_page(
                    body,
                    page_url=final_url,
                    company=self.company,
                    source=self.name,
                )
                if fallback:
                    return [fallback]
            unstructured += 1
            return []

        page_urls = sorted(enumeration.pages)[: self.max_pages]
        postings: list[Posting] = []
        batch_size = self.fetch_concurrency * 4
        for start in range(0, len(page_urls), batch_size):
            for recovered in await asyncio.gather(
                *(parse(url) for url in page_urls[start : start + batch_size])
            ):
                postings.extend(recovered)

        discovered = {item.canonical_apply_url: item for item in postings}
        career_surface_found = bool(
            enumeration.career_sitemap_urls or enumeration.reachable_career_landings
        )
        complete = career_surface_found and not enumeration.truncated
        if hard_errors and not discovered and not enumeration.reachable_career_landings:
            complete = False

        if complete and discovered:
            status = "ok"
        elif complete:
            status = "empty"
        elif discovered:
            status = "partial"
        elif blocked:
            status = "blocked"
        elif hard_errors:
            status = "broken"
        else:
            status = "unstructured"

        discovery_postings = [
            Posting(
                company=self.company,
                title="Employer careers surface",
                apply_url=url,
                source=self.name,
                source_id=url,
                source_mode="verification-lead",
            )
            for url in sorted(enumeration.provider_urls)
        ]
        note = (
            f"career graph: {enumeration.sitemap_documents} sitemaps, "
            f"{enumeration.career_sitemap_urls} career sitemap URLs, "
            f"{enumeration.feed_documents} feeds, "
            f"{enumeration.landing_pages} landing pages, "
            f"{enumeration.reachable_career_landings} career landings, "
            f"{len(page_urls)} candidate pages, "
            f"{len(enumeration.provider_urls)} provider links, {len(discovered)} jobs"
        )
        if enumeration.truncated:
            note += "; bounded crawl truncated"
        if blocked:
            note += f"; {blocked} blocked"
        if stale:
            note += f"; {stale} stale"
        if unstructured:
            note += f"; {unstructured} unstructured"
        if hard_errors:
            note += f"; {hard_errors} hard errors"

        return CollectorResult(
            source=self.name,
            postings=list(discovered.values()),
            complete=complete,
            mode=self.mode,
            rows_scanned=len(page_urls),
            expected_rows=len(page_urls) if complete else None,
            status=status,
            note=note,
            discovery_postings=discovery_postings,
        )
