from __future__ import annotations

import html
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import career_surface_collector as career

_EXTRA_SITEMAP_PATHS = (
    "/job-sitemap.xml",
    "/jobs-sitemap.xml",
    "/career-sitemap.xml",
    "/careers-sitemap.xml",
    "/sitemap-jobs.xml",
    "/sitemap-careers.xml",
    "/sitemap_jobs.xml",
    "/sitemap_careers.xml",
)

_EXTRA_PROVIDER_HOST_FRAGMENTS = {
    "bamboohr.com": "bamboohr",
    "hiringthing.com": "hiringthing",
    "applicantstack.com": "applicantstack",
    "exacthire.com": "exacthire",
    "hireology.com": "hireology",
    "newtonsoftware.com": "newton",
    "paylocity.com": "paylocity",
    "hirecentric.com": "hirecentric",
    "hrmdirect.com": "hrmdirect",
    "jobtarget.com": "jobtarget",
}

_URL_PATTERN = re.compile(
    r"(?P<url>(?:https?:)?(?:\\/\\/|//)[^\s\"'<>]+|/[A-Za-z0-9._~!$&()*+,;=:@%/-]*(?:job|career|intern|position|vacanc)[A-Za-z0-9._~!$&()*+,;=:@%/?#-]*)",
    re.IGNORECASE,
)

_INSTALLED = False
_ORIGINAL_LINKS = None


def _append_unique(existing: tuple[str, ...], additions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*existing, *additions)))


def _decode_script_candidate(raw: str) -> str:
    value = html.unescape(raw).replace("\\/", "/")
    if value.startswith("//"):
        return f"https:{value}"
    try:
        decoded = json.loads(f'"{value.replace(chr(34), chr(92) + chr(34))}"')
    except (json.JSONDecodeError, UnicodeDecodeError):
        decoded = value
    return str(decoded)


def _inline_script_links(body: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(body, "html.parser")
    output: dict[str, str] = {}
    for node in soup.find_all("script"):
        if node.get("src"):
            continue
        script_type = str(node.get("type") or "").casefold()
        if script_type == "application/ld+json":
            continue
        text = node.string or node.get_text("", strip=False)
        if not text:
            continue
        for match in _URL_PATTERN.finditer(text):
            raw = _decode_script_candidate(match.group("url")).rstrip("),;]}")
            normalized = career._normalized_http_url(urljoin(base_url, raw))
            if not normalized:
                continue
            if not (
                career.provider_kind(normalized)
                or career._careerish(normalized, "inline script config")
                or career._detailish(normalized)
            ):
                continue
            output[normalized] = "script:inline-config"
    return list(output.items())


def _expanded_links(body: str, base_url: str) -> list[tuple[str, str]]:
    assert _ORIGINAL_LINKS is not None
    output: dict[str, str] = dict(_ORIGINAL_LINKS(body, base_url))
    for url, label in _inline_script_links(body, base_url):
        output[url] = output.get(url) or label
    return list(output.items())


def install_runtime_coverage_extensions() -> None:
    """Install bounded production discovery for script configs, sitemaps, and ATS hosts."""

    global _INSTALLED, _ORIGINAL_LINKS
    if _INSTALLED:
        return
    _INSTALLED = True
    _ORIGINAL_LINKS = career._links
    career.CAREER_PATHS = _append_unique(career.CAREER_PATHS, _EXTRA_SITEMAP_PATHS)
    career.PROVIDER_HOST_FRAGMENTS.update(_EXTRA_PROVIDER_HOST_FRAGMENTS)
    career._links = _expanded_links


__all__ = ["install_runtime_coverage_extensions"]
