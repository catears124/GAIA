from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

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
_URL_ATTRIBUTE_NAMES = {"href", "url", "joburl", "job-url", "applyurl", "apply-url", "applicationurl", "application-url"}
_HTML_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_EXTRA_PROVIDER_HOST_FRAGMENTS = {
    "talent.com": "talent",
    "applytojob.com": "apply-to-job",
    "hirehive.com": "hirehive",
    "careers-page.com": "careers-page",
    "jobtrain.co.uk": "jobtrain",
    "eploy.net": "eploy",
    "vacancy-filler.co.uk": "vacancy-filler",
    "tribepad-gro.com": "tribepad",
    "hire.trakstar.com": "trakstar-hire",
    "jobs.personio.com": "personio",
    "onlyfy.io": "onlyfy",
    "softgarden.io": "softgarden",
    "prescreen.io": "prescreen",
    "recruitis.io": "recruitis",
}
_ORIGINAL_DOCUMENT_LINKS: Callable[[str, str], tuple[list[tuple[str, str]], bool]] | None = None
_INSTALLED = False


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].casefold()


def _attribute_value(node: ET.Element) -> str:
    for key, value in node.attrib.items():
        if key.rsplit("}", 1)[-1].casefold() in _URL_ATTRIBUTE_NAMES and str(value).strip():
            return str(value).strip()
    return ""


def _looks_like_url(raw: str) -> bool:
    value = raw.strip()
    if not value or any(char.isspace() for char in value):
        return False
    parts = urlsplit(value)
    return bool(parts.scheme in {"http", "https"} and parts.netloc) or value.startswith(("/", "./", "../", "//"))


def _candidate_urls(node: ET.Element) -> list[str]:
    values: list[str] = []
    attribute = _attribute_value(node)
    if attribute:
        values.append(attribute)
    text = str(node.text or "").strip()
    if text:
        if _looks_like_url(text):
            values.append(text)
        else:
            values.extend(match.rstrip("),.;") for match in _HTML_URL_RE.findall(text))
    return list(dict.fromkeys(values))


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
            rel = str(child.attrib.get("rel") or "").casefold()
            explicitly_url_shaped = tag in _URL_TAGS or bool(_attribute_value(child)) or rel in {"alternate", "related", "next"}
            if not explicitly_url_shaped:
                continue
            for raw in _candidate_urls(child):
                if tag == "guid" and not _looks_like_url(raw):
                    continue
                candidate = career._normalized_http_url(urljoin(base_url, raw))
                if candidate and (
                    career.provider_kind(candidate)
                    or career._careerish(candidate, tag)
                    or career._detailish(candidate)
                    or job_context
                    or rel == "next"
                ):
                    output[candidate] = tag if rel != "next" else "next"

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
