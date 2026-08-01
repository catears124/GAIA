from __future__ import annotations

import os
from collections import Counter
from urllib.parse import SplitResult, parse_qs, urlsplit

from .collectors import Collector
from .market_collectors import SitemapDomainCollector
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

PRIOR_YEAR_BLOCKED_HOSTS = {
    "github.com",
    "www.github.com",
    "simplify.jobs",
    "www.simplify.jobs",
    "speedyapply.com",
    "www.speedyapply.com",
    "discord.gg",
    "www.linkedin.com",
}
PRIOR_YEAR_HOSTED_PROVIDER_FRAGMENTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "oraclecloud.com",
    "icims.com",
    "jobvite.com",
    "workable.com",
    "recruitee.com",
    "rippling.com",
    "teamtailor.com",
)
PRIOR_YEAR_JOB_PATH_MARKERS = (
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
)


def _prefer(
    mapping: dict[str, tuple[str, str]],
    key: str,
    company: str,
    scope: str,
) -> None:
    existing = mapping.get(key)
    if existing is None or (existing[1] == "historical" and scope == "current"):
        mapping[key] = (company, scope)


def _is_prior_year_employer_domain(parts: SplitResult) -> bool:
    host = parts.netloc.lower().split(":", 1)[0]
    path = parts.path.casefold()
    if parts.scheme not in {"http", "https"} or not host:
        return False
    if host in PRIOR_YEAR_BLOCKED_HOSTS:
        return False
    if any(fragment in host for fragment in PRIOR_YEAR_HOSTED_PROVIDER_FRAGMENTS):
        return False
    return host.startswith(("jobs.", "careers.")) or any(
        marker in path for marker in PRIOR_YEAR_JOB_PATH_MARKERS
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
    prior_year_domains: dict[str, Counter[str]] = {}
    discover_prior_year_domains = os.getenv("GAIA_PRIOR_YEAR_DOMAINS", "1") == "1"

    for posting in postings:
        parts = urlsplit(posting.apply_url)
        host = parts.netloc.lower().split(":", 1)[0]
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

        if (
            discover_prior_year_domains
            and scope == "historical"
            and _is_prior_year_employer_domain(parts)
        ):
            companies = prior_year_domains.setdefault(host, Counter())
            companies[canonical_company(posting.company)] += 1

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
    for host, companies in prior_year_domains.items():
        company = companies.most_common(1)[0][0]
        # Do not re-fetch stale 2026 job URLs. Probe the employer's current robots/sitemaps instead.
        collector = SitemapDomainCollector(company, host, [])
        collector.scope = "historical"
        collectors.append(collector)
    return collectors
