from __future__ import annotations

from urllib.parse import urlsplit

from .collectors import Collector
from .models import Posting
from .provider_collectors import RecruiteeCollector, SmartRecruitersCollector, WorkableCollector


def _prefer(
    mapping: dict[str, tuple[str, str]],
    key: str,
    company: str,
    scope: str,
) -> None:
    existing = mapping.get(key)
    if existing is None or (existing[1] == "historical" and scope == "current"):
        mapping[key] = (company, scope)


def provider_collectors_from_postings(postings: list[Posting]) -> list[Collector]:
    smartrecruiters: dict[str, tuple[str, str]] = {}
    recruitee: dict[str, tuple[str, str]] = {}
    workable: dict[str, tuple[str, str]] = {}

    for posting in postings:
        parts = urlsplit(posting.apply_url)
        host = parts.netloc.lower().split(":", 1)[0]
        segments = [segment for segment in parts.path.split("/") if segment]
        scope = "historical" if posting.source_mode == "universe-seed" else "current"

        if host == "jobs.smartrecruiters.com" and segments:
            _prefer(smartrecruiters, segments[0], posting.company, scope)
            continue

        if host.endswith(".recruitee.com") and host not in {
            "api.recruitee.com",
            "docs.recruitee.com",
        }:
            subdomain = host[: -len(".recruitee.com")].split(".")[-1]
            if subdomain and subdomain not in {"www", "api"}:
                _prefer(recruitee, subdomain, posting.company, scope)
            continue

        if host == "apply.workable.com" and segments:
            _prefer(workable, segments[0], posting.company, scope)
            continue
        if host.endswith(".workable.com") and host not in {
            "www.workable.com",
            "apply.workable.com",
        }:
            subdomain = host[: -len(".workable.com")].split(".")[-1]
            if subdomain:
                _prefer(workable, subdomain, posting.company, scope)

    collectors: list[Collector] = []
    for identifier, (company, scope) in smartrecruiters.items():
        collector = SmartRecruitersCollector(company, identifier)
        collector.scope = scope
        collectors.append(collector)
    for subdomain, (company, scope) in recruitee.items():
        collector = RecruiteeCollector(company, subdomain)
        collector.scope = scope
        collectors.append(collector)
    for subdomain, (company, scope) in workable.items():
        collector = WorkableCollector(company, subdomain)
        collector.scope = scope
        collectors.append(collector)
    return collectors
