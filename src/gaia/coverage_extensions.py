from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import career_surface_collector as career

_EXTRA_CAREER_PATHS = (
    "/internships",
    "/students",
    "/student-opportunities",
    "/student-careers",
    "/graduates",
    "/graduate-programs",
    "/campus",
    "/campus-recruiting",
    "/early-careers",
    "/early-talent",
    "/emerging-talent",
    "/university",
    "/university-recruiting",
    "/university-programs",
    "/careers/search",
    "/careers/jobs",
    "/jobs/search",
    "/job-search",
    "/open-roles",
    "/open-positions",
    "/vacancies",
    "/jobs/feed",
    "/careers/feed",
    "/jobs.rss",
    "/careers.rss",
    "/jobs.xml",
    "/careers.xml",
)
_EXTRA_CAREER_MARKERS = (
    "internship",
    "internships",
    "student-career",
    "student-opportunit",
    "graduate-program",
    "campus-recruit",
    "early-career",
    "early-talent",
    "emerging-talent",
    "university-recruit",
    "university-program",
    "job-search",
    "search-jobs",
    "open-roles",
    "open-positions",
    "job-board",
    "job-listing",
)
_EXTRA_DETAIL_MARKERS = (
    "/job-detail/",
    "/job-details/",
    "/posting/",
    "/postings/",
    "/position/",
    "/positions/",
    "/vacancy/",
    "/vacancies/",
    "/role/",
    "/roles/",
    "/opportunity/",
    "/opportunities/",
)
_EXTRA_PROVIDER_HOST_FRAGMENTS = {
    "breezy.hr": "breezy",
    "applytojob.com": "jazzhr",
    "personio.com": "personio",
    "jobs.personio.de": "personio",
    "comeet.co": "comeet",
    "pinpointhq.com": "pinpoint",
    "applicantpro.com": "applicantpro",
    "trakstar.com": "trakstar-hire",
    "hire.trakstar.com": "trakstar-hire",
    "careers-page.com": "manatal",
    "manatal.com": "manatal",
    "workable.com": "workable",
    "recruitee.com": "recruitee",
    "jobvite.com": "jobvite",
    "icims.com": "icims",
    "oraclecloud.com": "oracle-recruiting",
    "successfactors.com": "successfactors",
    "smartsearchonline.com": "smartsearch",
    "catsone.com": "cats",
    "jazz.co": "jazzhr",
    "jobsoid.com": "jobsoid",
    "occupop.com": "occupop",
    "hrcloud.com": "hrcloud",
    "talentclue.com": "talentclue",
    "join.com": "join",
    "recruiterbox.com": "recruiterbox",
    "zohorecruit.com": "zoho-recruit",
    "eightfold.ai": "eightfold",
    "phenompeople.com": "phenom",
    "avature.net": "avature",
    "dayforcehcm.com": "dayforce",
    "paycomonline.net": "paycom",
    "ultipro.com": "ukg",
    "myworkday.com": "workday",
    "taleo.net": "taleo",
    "brassring.com": "brassring",
    "csod.com": "cornerstone",
    "adp.com": "adp-workforce-now",
    "jobadder.com": "jobadder",
    "tribepad.com": "tribepad",
    "fountain.com": "fountain",
}
_EMBEDDED_URL_ATTRIBUTES = (
    ("iframe", "src"),
    ("form", "action"),
    ("script", "src"),
    ("embed", "src"),
    ("object", "data"),
)
_DATA_URL_ATTRIBUTES = (
    "data-url",
    "data-src",
    "data-href",
    "data-job-url",
    "data-careers-url",
    "data-apply-url",
)
_STRUCTURED_URL_KEYS = {
    "url",
    "sameAs",
    "applicationUrl",
    "applyUrl",
    "target",
}

_ORIGINAL_LINKS = career._links
_INSTALLED = False


def _append_unique(existing: tuple[str, ...], additions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*existing, *additions)))


def _schema_types(value: object) -> set[str]:
    if isinstance(value, list):
        return {str(item).casefold() for item in value}
    if value is None:
        return set()
    return {str(value).casefold()}


def _structured_values(
    value: object,
    *,
    inherited_job_context: bool = False,
) -> Iterable[str]:
    """Yield application surfaces while retaining JobPosting context through nesting."""

    if isinstance(value, Mapping):
        job_context = inherited_job_context or "jobposting" in _schema_types(value.get("@type"))
        for key, item in value.items():
            if key in _STRUCTURED_URL_KEYS and isinstance(item, str):
                if job_context or "job" in item.casefold() or "career" in item.casefold():
                    yield item
            yield from _structured_values(item, inherited_job_context=job_context)
    elif isinstance(value, list):
        for item in value:
            yield from _structured_values(item, inherited_job_context=inherited_job_context)


def _structured_surface_links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    output: dict[str, str] = {}
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = node.string or node.get_text("", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for candidate in _structured_values(payload):
            normalized = career._normalized_http_url(urljoin(base_url, candidate))
            if normalized and (
                career.provider_kind(normalized)
                or career._careerish(normalized, "json-ld jobposting")
                or career._detailish(normalized)
            ):
                output[normalized] = "json-ld:JobPosting"

    for node in soup.find_all("meta", attrs={"http-equiv": True, "content": True}):
        if str(node.get("http-equiv") or "").casefold() != "refresh":
            continue
        content = str(node.get("content") or "")
        marker = content.casefold().find("url=")
        if marker < 0:
            continue
        candidate = content[marker + 4 :].strip(" \"'")
        normalized = career._normalized_http_url(urljoin(base_url, candidate))
        if normalized and (
            career.provider_kind(normalized)
            or career._careerish(normalized, "meta refresh careers")
        ):
            output[normalized] = "meta:refresh"

    for node in soup.find_all(attrs={"itemprop": True}):
        itemprop = str(node.get("itemprop") or "").casefold()
        if itemprop not in {"url", "applicationurl", "applyurl"}:
            continue
        raw = str(node.get("href") or node.get("content") or node.get("value") or "").strip()
        normalized = career._normalized_http_url(urljoin(base_url, raw)) if raw else None
        if normalized and (
            career.provider_kind(normalized)
            or career._careerish(normalized, "schema jobposting")
            or career._detailish(normalized)
        ):
            output[normalized] = f"microdata:{itemprop}"
    return list(output.items())


def _embedded_surface_links(body: str, base_url: str) -> list[tuple[str, str]]:
    """Recover ATS and career URLs hidden outside ordinary anchor elements."""

    soup = BeautifulSoup(body, "html.parser")
    output: dict[str, str] = dict(_structured_surface_links(soup, base_url))
    for tag_name, attribute in _EMBEDDED_URL_ATTRIBUTES:
        for node in soup.find_all(tag_name):
            raw = str(node.get(attribute) or "").strip()
            if not raw:
                continue
            normalized = career._normalized_http_url(urljoin(base_url, raw))
            if normalized and (
                career.provider_kind(normalized)
                or career._careerish(normalized, f"{tag_name} {attribute}")
            ):
                output[normalized] = f"{tag_name}:{attribute}"

    for node in soup.find_all(True):
        for attribute in _DATA_URL_ATTRIBUTES:
            raw = str(node.get(attribute) or "").strip()
            if not raw:
                continue
            normalized = career._normalized_http_url(urljoin(base_url, raw))
            if normalized and (
                career.provider_kind(normalized)
                or career._careerish(normalized, attribute)
            ):
                output[normalized] = attribute
    return list(output.items())


def _expanded_links(body: str, base_url: str) -> list[tuple[str, str]]:
    output: dict[str, str] = dict(_ORIGINAL_LINKS(body, base_url))
    for url, label in _embedded_surface_links(body, base_url):
        output[url] = output.get(url) or label
    return list(output.items())


def install_coverage_extensions() -> None:
    """Expand bounded discovery without inventing or duplicating source records."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    career.CAREER_PATHS = _append_unique(career.CAREER_PATHS, _EXTRA_CAREER_PATHS)
    career.CAREER_MARKERS = _append_unique(career.CAREER_MARKERS, _EXTRA_CAREER_MARKERS)
    career.DETAIL_MARKERS = _append_unique(career.DETAIL_MARKERS, _EXTRA_DETAIL_MARKERS)
    career.PROVIDER_HOST_FRAGMENTS.update(_EXTRA_PROVIDER_HOST_FRAGMENTS)
    career._links = _expanded_links


__all__ = ["install_coverage_extensions"]
