from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import career_surface_collector as career

_EXTRA_MACHINE_FEED_PATHS = (
    "/jobs.json",
    "/careers.json",
    "/job-board.json",
    "/api/jobs",
    "/api/v1/jobs",
    "/api/v2/jobs",
    "/api/job-postings",
    "/api/requisitions",
)

_EXTRA_PROVIDER_HOST_FRAGMENTS = {
    "jobs.dover.com": "dover",
    "jobs.gem.com": "gem",
    "jobs.polymer.co": "polymer",
    "homerun.co": "homerun",
    "screenloop.com": "screenloop",
    "jobs.kula.ai": "kula",
    "jobs.gusto.com": "gusto",
    "recruitingbypaycor.com": "paycor",
}

_ORIGINAL_XML_LOCATIONS = career._xml_locations
_INSTALLED = False


@dataclass(frozen=True, slots=True)
class _SitemapEntry:
    location: str
    last_modified: str
    ordinal: int


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _freshness_ordered_xml_locations(body: str) -> tuple[list[str], bool]:
    """Prefer recently modified sitemap entries when a large board exceeds crawl bounds.

    GAIA intentionally caps each employer crawl. Many CMS and ATS sitemaps are ordered
    oldest-first, so consuming document order can fill that cap with stale jobs and miss
    today's postings. Valid ISO-8601 lastmod values sort lexicographically; entries with
    no lastmod retain their original order after dated entries.
    """

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [], False

    is_index = _local_name(root.tag) == "sitemapindex"
    entries: list[_SitemapEntry] = []
    containers = [
        node
        for node in root
        if _local_name(node.tag) in {"url", "sitemap", "item", "entry"}
    ]
    for ordinal, container in enumerate(containers):
        location = ""
        last_modified = ""
        for child in container:
            name = _local_name(child.tag)
            value = (child.text or "").strip()
            if name in {"loc", "link"} and not location:
                location = str(child.attrib.get("href") or value).strip()
            elif name in {"lastmod", "updated", "pubdate", "published"}:
                last_modified = value
        if location:
            entries.append(_SitemapEntry(location, last_modified, ordinal))

    if not entries:
        return _ORIGINAL_XML_LOCATIONS(body)

    entries.sort(
        key=lambda item: (
            bool(item.last_modified),
            item.last_modified,
            -item.ordinal,
        ),
        reverse=True,
    )
    return list(dict.fromkeys(item.location for item in entries)), is_index


def _append_unique(existing: tuple[str, ...], additions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*existing, *additions)))


def install_freshness_coverage_extension() -> None:
    """Install freshness-first sitemap selection and additional machine-readable feeds."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    career.CAREER_PATHS = _append_unique(career.CAREER_PATHS, _EXTRA_MACHINE_FEED_PATHS)
    career.PROVIDER_HOST_FRAGMENTS.update(_EXTRA_PROVIDER_HOST_FRAGMENTS)
    career._xml_locations = _freshness_ordered_xml_locations


__all__ = ["install_freshness_coverage_extension"]
