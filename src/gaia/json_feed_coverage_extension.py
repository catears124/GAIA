from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin

from . import career_surface_collector as career

_URL_KEYS = {
    "url",
    "absoluteurl",
    "hostedurl",
    "joburl",
    "detailurl",
    "externalurl",
    "applyurl",
    "applicationurl",
    "applicationlink",
    "applylink",
    "redirecturl",
    "next",
    "nexturl",
    "nextpage",
    "nextpageurl",
}
_JOB_CONTAINER_KEYS = {
    "jobs",
    "jobpostings",
    "postings",
    "positions",
    "openings",
    "requisitions",
    "vacancies",
    "results",
    "items",
    "data",
}
_EXTRA_PROVIDER_HOST_FRAGMENTS = {
    "crelate.com": "crelate",
    "recruitcrm.io": "recruit-crm",
    "loxo.co": "loxo",
    "vincere.io": "vincere",
    "bullhornstaffing.com": "bullhorn",
    "ceipal.com": "ceipal",
    "trackerrms.com": "tracker-rms",
    "pcrecruiter.net": "pcrecruiter",
}
_ORIGINAL_DOCUMENT_LINKS = career._document_links
_INSTALLED = False


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _json_links(body: str, base_url: str) -> tuple[list[tuple[str, str]], bool]:
    """Recover job-detail, application, and pagination URLs from generic JSON APIs."""

    stripped = body.lstrip()
    if not stripped.startswith(("{", "[")):
        return [], False
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return [], False

    output: dict[str, str] = {}
    job_feed = False
    visited = 0
    stack: list[tuple[object, str, bool]] = [(payload, "", False)]
    while stack and visited < 20_000:
        value, parent_key, in_job_container = stack.pop()
        visited += 1
        if isinstance(value, Mapping):
            normalized_keys = {_normalized_key(key) for key in value}
            record_like = bool(
                normalized_keys & {"title", "jobtitle", "positiontitle", "name"}
                and normalized_keys & {"id", "jobid", "requisitionid", "reqid", "url", "applyurl"}
            )
            current_job_context = in_job_container or record_like
            if record_like:
                job_feed = True
            for key, child in value.items():
                normalized = _normalized_key(key)
                child_job_context = current_job_context or normalized in {
                    _normalized_key(item) for item in _JOB_CONTAINER_KEYS
                }
                if normalized in {_normalized_key(item) for item in _JOB_CONTAINER_KEYS}:
                    job_feed = True
                if isinstance(child, str) and normalized in _URL_KEYS:
                    candidate = career._normalized_http_url(urljoin(base_url, child.strip()))
                    if candidate and (
                        career.provider_kind(candidate)
                        or career._careerish(candidate, str(key))
                        or career._detailish(candidate)
                        or normalized.startswith("next")
                        or current_job_context
                    ):
                        output[candidate] = str(key)
                elif isinstance(child, (Mapping, list, tuple)):
                    stack.append((child, normalized, child_job_context))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in reversed(value):
                stack.append((child, parent_key, in_job_container))

    return list(output.items()), job_feed


def _document_links_with_json(body: str, base_url: str) -> tuple[list[tuple[str, str]], bool]:
    links, is_feed = _ORIGINAL_DOCUMENT_LINKS(body, base_url)
    json_links, is_job_feed = _json_links(body, base_url)
    merged = {url: label for url, label in links}
    for url, label in json_links:
        merged[url] = merged.get(url) or label
    return list(merged.items()), is_feed or is_job_feed


def install_json_feed_coverage_extension() -> None:
    """Install generic JSON job-feed traversal and additional ATS recognition."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    career.PROVIDER_HOST_FRAGMENTS.update(_EXTRA_PROVIDER_HOST_FRAGMENTS)
    career._document_links = _document_links_with_json


__all__ = ["install_json_feed_coverage_extension"]
