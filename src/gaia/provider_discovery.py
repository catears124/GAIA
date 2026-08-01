from __future__ import annotations

import os
from collections import Counter
from urllib.parse import SplitResult, parse_qs, urlsplit

from .career_surface_collector import CareerSurfaceCollector, provider_kind
from .collectors import Collector
from .models import Posting
from .provider_collectors import (
    ICIMSCollector,
    JobviteCollector,
    OracleCloudCollector,
    RecruiteeCollector,
    SmartRecruitersCollector,
    SuccessFactorsCollector,
    WorkableCollector,
)
from .quality import canonical_company
from .rippling_collector import RipplingCollector
from .teamtailor_collector import TeamtailorCollector

BLOCKED_DISCOVERY_HOSTS = {
    "github.com",
    "www.github.com",
    "simplify.jobs",
    "www.simplify.jobs",
    "speedyapply.com",
    "www.speedyapply.com",
    "discord.gg",
    "linkedin.com",
    "www.linkedin.com",
    "indeed.com",
    "www.indeed.com",
    "glassdoor.com",
    "www.glassdoor.com",
    "ziprecruiter.com",
    "www.ziprecruiter.com",
    "jobright.ai",
    "www.jobright.ai",
    "workopia.io",
    "www.workopia.io",
    "ycombinator.com",
    "www.ycombinator.com",
}
NATIVE_PROVIDER_KINDS = {
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "recruitee",
    "workable",
    "jobvite",
    "icims",
    "oracle-cloud",
    "successfactors",
    "rippling",
    "teamtailor",
}
JOB_PATH_MARKERS = (
    "/job/",
    "/jobs/",
    "/career/",
    "/careers/",
    "/position/",
    "/positions/",
    "/opening/",
    "/openings/",
    "/requisition/",
    "/requisitions/",
    "/apply/",
    "/details/",
    "/join-us",
    "/work-with-us",
    "/open-roles",
    "/open-positions",
)


def _prefer(
    mapping: dict[object, tuple[str, str]],
    key: object,
    company: str,
    scope: str,
) -> None:
    existing = mapping.get(key)
    if existing is None or (existing[1] == "historical" and scope == "current"):
        mapping[key] = (company, scope)


def _is_employer_hiring_surface(parts: SplitResult, *, source_mode: str) -> bool:
    host = parts.netloc.casefold().split(":", 1)[0]
    path = parts.path.casefold()
    if parts.scheme not in {"http", "https"} or not host or host in BLOCKED_DISCOVERY_HOSTS:
        return False
    if provider_kind(parts.geturl()):
        return True
    if source_mode == "ecosystem-observation":
        return True
    return host.startswith(("jobs.", "careers.", "work.")) or any(
        marker in path for marker in JOB_PATH_MARKERS
    )


def _domain_key(parts: SplitResult, company: str) -> tuple[str, str]:
    host = parts.netloc.casefold().split(":", 1)[0]
    kind = provider_kind(parts.geturl())
    if kind:
        # Unsupported hosted ATS products can put many employers behind one host.
        # Keep those tenant-scoped until a first-class parser can extract the ATS key.
        canonical = canonical_company(company) or company.strip()
        return host, canonical.casefold()
    return host, ""


def _register_domain(
    mapping: dict[tuple[str, str], dict[str, object]],
    *,
    company: str,
    parts: SplitResult,
    scope: str,
    seed_url: str,
) -> None:
    canonical = canonical_company(company) or company.strip()
    host = parts.netloc.casefold().split(":", 1)[0]
    if not canonical or not host:
        return
    key = _domain_key(parts, canonical)
    item = mapping.setdefault(
        key,
        {
            "host": host,
            "scope": scope,
            "seeds": set(),
            "company_votes": Counter(),
            "current_votes": Counter(),
        },
    )
    if item["scope"] == "historical" and scope == "current":
        item["scope"] = "current"
    seeds = item["seeds"]
    company_votes = item["company_votes"]
    current_votes = item["current_votes"]
    assert isinstance(seeds, set)
    assert isinstance(company_votes, Counter)
    assert isinstance(current_votes, Counter)
    seeds.add(seed_url)
    company_votes[canonical] += 1
    if scope == "current":
        current_votes[canonical] += 1


def _domain_company(item: dict[str, object]) -> str:
    company_votes = item["company_votes"]
    current_votes = item["current_votes"]
    assert isinstance(company_votes, Counter)
    assert isinstance(current_votes, Counter)
    return max(
        company_votes,
        key=lambda company: (
            current_votes[company],
            company_votes[company],
            -len(company),
            company.casefold(),
        ),
    )


def provider_collectors_from_postings(postings: list[Posting]) -> list[Collector]:
    smartrecruiters: dict[str, tuple[str, str]] = {}
    recruitee: dict[str, tuple[str, str]] = {}
    workable: dict[str, tuple[str, str]] = {}
    jobvite: dict[str, tuple[str, str]] = {}
    icims: dict[str, tuple[str, str]] = {}
    rippling: dict[str, tuple[str, str]] = {}
    teamtailor: dict[str, tuple[str, str]] = {}
    oracle: dict[tuple[str, str], tuple[str, str]] = {}
    successfactors: dict[tuple[str, str], tuple[str, str]] = {}
    domain_surfaces: dict[tuple[str, str], dict[str, object]] = {}
    discover_domains = os.getenv("GAIA_EMPLOYER_DOMAIN_DISCOVERY", "1") == "1"

    for posting in postings:
        parts = urlsplit(posting.apply_url)
        host = parts.netloc.casefold().split(":", 1)[0]
        segments = [segment for segment in parts.path.split("/") if segment]
        scope = "historical" if posting.source_mode == "universe-seed" else "current"

        if host == "jobs.smartrecruiters.com" and segments:
            _prefer(smartrecruiters, segments[0], posting.company, scope)
            continue

        if host == "jobs.jobvite.com" and segments:
            _prefer(jobvite, segments[0], posting.company, scope)
            continue

        if host == "ats.rippling.com" and segments:
            slug_index = 1 if segments[0].casefold() in {
                "en-us",
                "en-gb",
                "es-419",
                "fr-fr",
                "de-de",
            } else 0
            if slug_index < len(segments):
                _prefer(rippling, segments[slug_index], posting.company, scope)
            continue

        if host.endswith(".teamtailor.com") and host not in {
            "www.teamtailor.com",
            "support.teamtailor.com",
        }:
            subdomain = host[: -len(".teamtailor.com")].split(".")[-1]
            if subdomain:
                _prefer(teamtailor, subdomain, posting.company, scope)
            continue

        if host.endswith(".icims.com") and "jobs" in segments:
            _prefer(icims, host, posting.company, scope)
            continue

        if "oraclecloud.com" in host and "sites" in segments:
            site_index = segments.index("sites")
            if site_index + 1 < len(segments):
                key = (f"{parts.scheme}://{host}", segments[site_index + 1])
                _prefer(oracle, key, posting.company, scope)
                continue

        if "successfactors." in host:
            company_id = (parse_qs(parts.query).get("company") or [""])[0]
            if company_id:
                key = (f"{parts.scheme}://{host}", company_id)
                _prefer(successfactors, key, posting.company, scope)
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
            continue

        kind = provider_kind(parts.geturl())
        if kind in NATIVE_PROVIDER_KINDS:
            # Native registry discovery owns these. A second generic source would
            # duplicate work and can cross tenant boundaries on shared ATS hosts.
            continue

        if (
            discover_domains
            and _is_employer_hiring_surface(parts, source_mode=posting.source_mode)
        ):
            _register_domain(
                domain_surfaces,
                company=posting.company,
                parts=parts,
                scope=scope,
                seed_url=posting.apply_url,
            )

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
    for slug, (company, scope) in jobvite.items():
        collector = JobviteCollector(company, slug)
        collector.scope = scope
        collectors.append(collector)
    for host, (company, scope) in icims.items():
        collector = ICIMSCollector(company, host)
        collector.scope = scope
        collectors.append(collector)
    for slug, (company, scope) in rippling.items():
        collector = RipplingCollector(company, slug)
        collector.scope = scope
        collectors.append(collector)
    for subdomain, (company, scope) in teamtailor.items():
        collector = TeamtailorCollector(company, subdomain)
        collector.scope = scope
        collectors.append(collector)
    for (origin, site), (company, scope) in oracle.items():
        collector = OracleCloudCollector(company, origin, site)
        collector.scope = scope
        collectors.append(collector)
    for (origin, company_id), (company, scope) in successfactors.items():
        collector = SuccessFactorsCollector(company, origin, company_id)
        collector.scope = scope
        collectors.append(collector)
    for item in domain_surfaces.values():
        seeds = item["seeds"]
        assert isinstance(seeds, set)
        collector = CareerSurfaceCollector(
            _domain_company(item),
            str(item["host"]),
            sorted(str(seed) for seed in seeds),
        )
        collector.scope = str(item["scope"])
        collectors.append(collector)
    return collectors
