from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import career_surface_collector as career

_EXTRA_PROVIDER_HOST_FRAGMENTS = {
    "talentbrew.com": "talentbrew",
    "jobs2web.com": "jobs2web",
    "radancy.com": "radancy",
    "symplicity.com": "symplicity",
    "12twenty.com": "12twenty",
    "simplicant.com": "simplicant",
    "manatal.com": "manatal",
    "comeet.com": "comeet",
}
_CAREER_SUBDOMAIN_PREFIXES = (
    "jobs.",
    "job.",
    "careers.",
    "career.",
    "recruiting.",
    "recruitment.",
    "talent.",
    "join.",
    "work.",
)
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "vero_conv",
    "vero_id",
}
_ORIGINAL_SAME_HOST: Callable[[str, str], bool] | None = None
_ORIGINAL_NORMALIZED_HTTP_URL: Callable[[str], str | None] | None = None
_INSTALLED = False


def _host(value: str) -> str:
    return value.casefold().split(":", 1)[0].strip().strip(".")


def _is_career_subdomain(host: str) -> bool:
    return host.startswith(_CAREER_SUBDOMAIN_PREFIXES)


def _same_employer_surface(url: str, seed_host: str) -> bool:
    target = _host(urlsplit(url).netloc)
    seed = _host(seed_host)
    if not target or not seed:
        return False
    if target == seed:
        return True
    target_bare = target.removeprefix("www.")
    seed_bare = seed.removeprefix("www.")
    if target_bare == seed_bare:
        return True
    if career._registrable_domain(target) != career._registrable_domain(seed):
        return False
    return _is_career_subdomain(target) or _is_career_subdomain(seed)


def _strip_tracking_query(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept, doseq=True), parts.fragment))


def _normalized_without_tracking(url: str) -> str | None:
    assert _ORIGINAL_NORMALIZED_HTTP_URL is not None
    normalized = _ORIGINAL_NORMALIZED_HTTP_URL(url)
    if normalized is None:
        return None
    return _strip_tracking_query(normalized)


def install_domain_graph_coverage_extension() -> None:
    global _INSTALLED, _ORIGINAL_SAME_HOST, _ORIGINAL_NORMALIZED_HTTP_URL
    if _INSTALLED:
        return
    _ORIGINAL_SAME_HOST = career._same_host
    _ORIGINAL_NORMALIZED_HTTP_URL = career._normalized_http_url
    career.PROVIDER_HOST_FRAGMENTS.update(_EXTRA_PROVIDER_HOST_FRAGMENTS)
    career._same_host = _same_employer_surface
    career._normalized_http_url = _normalized_without_tracking
    _INSTALLED = True


__all__ = ["install_domain_graph_coverage_extension"]
