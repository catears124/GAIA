from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from urllib.parse import urljoin

from . import career_surface_collector as career

_JOB_RECORD_TAGS = {"job", "position", "posting", "opening", "requisition", "vacancy", "item", "entry"}
_URL_TAGS = {
    "url",
    "joburl",
    "job-url",
    "detailurl",
    "detail-url",
    "applyurl",
    "apply-url",
    "applicationurl",
    "application-url",
    "link",
    "guid",
    "loc",
}
_TITLE_TAGS = {"title", "jobtitle", "job-title", "positiontitle", "position-title"}
_ID_TAGS = {"id", "jobid", "job-id", "requisitionid", "requisition-id", "reference"}
_EXTRA_PROVIDER_HOST_FRAGMENTS = {
    "talent.com": "talent",
    "applytojob.com": "apply-to-job",
    "hirehive.com": "hirehive",
    "careers-page.com": "careers-page",
    "jobtrain.co.uk": "jobtrain",
    "eploy.net": "eploy",
    "vacancy-filler.co.uk": "vacancy-filler",
    "tribepad-gro.com": "tribepad",
}
_ORIGINAL_DOCUMENT_LINKS: Callable[[str, str], tuple[list[tuple[str, str]], bool]] | None = None
_INSTALLED = False


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].casefold()


def _xml_job_links(body: str, base_url: str) -> tuple[list[tuple[str, str]], bool]:
    stripped = body.lstrip()
    if not stripped.startswith("<"):
        return [], False
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [], False

    output: dict[str, str] = {}
    root_tag = _tag(root)
    feed_like = root_tag in {"rss", "feed", "rdf", "jobs", "jobfeed", "job-feed", "positions", "vacancies"}

    for record in root.iter():
        record_tag = _tag(record)
        if record_tag not in _JOB_RECORD_TAGS:
            continue
        child_tags = {_tag(child) for child in record.iter()}
        job_context = record_tag in {"job", "position", "posting", "opening", "requisition", "vacancy"} or bool(
            child_tags & _TITLE_TAGS and child_tags & (_ID_TAGS | _URL_TAGS)
        )
        if job_context:
            feed_like = True
        for child in record.iter():
            tag = _tag(child)
            if tag not in _URL_TAGS:
                continue
            raw = str(child.attrib.get("href") or child.text or "").strip()
            if not raw:
                continue
            candidate = career._normalized_http_url(urljoin(base_url, raw))
            if candidate and (
                career.provider_kind(candidate)
                or career._careerish(candidate, tag)
                or career._detailish(candidate)
                or job_context
            ):
                output[candidate] = tag

    return list(output.items()), feed_like


def _document_links_with_xml_jobs(body: str, base_url: str) -> tuple[list[tuple[str, str]], bool]:
    assert _ORIGINAL_DOCUMENT_LINKS is not None
    links, is_feed = _ORIGINAL_DOCUMENT_LINKS(body, base_url)
    xml_links, is_job_feed = _xml_job_links(body, base_url)
    merged = {url: label for url, label in links}
    for url, label in xml_links:
        merged[url] = merged.get(url) or label
    return list(merged.items()), is_feed or is_job_feed


def install_xml_feed_coverage_extension() -> None:
    global _INSTALLED, _ORIGINAL_DOCUMENT_LINKS
    if _INSTALLED:
        return
    _ORIGINAL_DOCUMENT_LINKS = career._document_links
    career.PROVIDER_HOST_FRAGMENTS.update(_EXTRA_PROVIDER_HOST_FRAGMENTS)
    career._document_links = _document_links_with_xml_jobs
    _INSTALLED = True


__all__ = ["install_xml_feed_coverage_extension"]
