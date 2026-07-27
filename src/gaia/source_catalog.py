from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from .collectors import (
    AshbyCollector,
    Collector,
    GreenhouseCollector,
    LeverCollector,
    SchemaPageCollector,
)
from .db import Database
from .market_collectors import SitemapDomainCollector, WorkdaySearchCollector
from .models import Posting
from .native_collectors import GoogleInternshipCollector
from .provider_collectors import (
    ICIMSCollector,
    JobviteCollector,
    OracleCloudCollector,
    RecruiteeCollector,
    SmartRecruitersCollector,
    SuccessFactorsCollector,
    WorkableCollector,
)
from .quality import canonical_source_name, is_actionable_application_url


def _spec(collector: Collector) -> tuple[str, dict[str, Any]] | None:
    if isinstance(collector, GreenhouseCollector):
        return "greenhouse", {"company": collector.company, "board": collector.board}
    if isinstance(collector, LeverCollector):
        return "lever", {"company": collector.company, "site": collector.site}
    if isinstance(collector, AshbyCollector):
        return "ashby", {"company": collector.company, "board": collector.board}
    if isinstance(collector, WorkdaySearchCollector):
        return "workday-search", {
            "company": collector.company,
            "host": collector.host,
            "tenant": collector.tenant,
            "site": collector.site,
            "terms": list(collector.terms),
        }
    if isinstance(collector, SmartRecruitersCollector):
        return "smartrecruiters", {
            "company": collector.company,
            "identifier": collector.identifier,
        }
    if isinstance(collector, RecruiteeCollector):
        return "recruitee", {
            "company": collector.company,
            "subdomain": collector.subdomain,
        }
    if isinstance(collector, WorkableCollector):
        return "workable", {
            "company": collector.company,
            "subdomain": collector.subdomain,
        }
    if isinstance(collector, JobviteCollector):
        return "jobvite", {"company": collector.company, "slug": collector.slug}
    if isinstance(collector, ICIMSCollector):
        return "icims", {"company": collector.company, "host": collector.host}
    if isinstance(collector, OracleCloudCollector):
        return "oracle-cloud", {
            "company": collector.company,
            "origin": collector.origin,
            "site": collector.site,
        }
    if isinstance(collector, SuccessFactorsCollector):
        return "successfactors", {
            "company": collector.company,
            "origin": collector.origin,
            "company_id": collector.company_id,
        }
    if isinstance(collector, SitemapDomainCollector):
        return "domain", {
            "company": collector.company,
            "host": collector.host,
            "seed_urls": collector.seed_urls,
        }
    if isinstance(collector, SchemaPageCollector):
        return "verification", {
            "company": collector.company,
            "urls": collector.urls,
            "trusted": collector.source_mode == "verification",
            "leads": [
                {
                    "title": item.title,
                    "url": item.apply_url,
                    "source_id": item.source_id,
                    "locations": item.locations,
                }
                for item in collector.leads
            ],
            "name": canonical_source_name(collector.name),
        }
    if isinstance(collector, GoogleInternshipCollector):
        return "google-careers", {}
    return None


def save_catalog(database: Database, collectors: list[Collector]) -> int:
    rows = []
    for collector in collectors:
        described = _spec(collector)
        if described is None:
            continue
        kind, spec = described
        rows.append(
            (
                canonical_source_name(collector.name),
                kind,
                collector.scope,
                Jsonb(spec),
            )
        )
    if not rows:
        return 0
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO source_catalog(source, kind, scope, spec)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(source) DO UPDATE SET
                kind=excluded.kind,
                scope=CASE
                    WHEN source_catalog.scope='current' THEN 'current'
                    ELSE excluded.scope
                END,
                spec=excluded.spec,
                last_discovered_at=now()
            """,
            rows,
        )
        connection.execute(
            """
            UPDATE source_catalog AS catalog
            SET scope='current', last_discovered_at=now()
            FROM source_health AS health
            WHERE health.source=catalog.source
              AND health.lifecycle='productive'
            """
        )
    return len(rows)


def _collector(kind: str, spec: dict[str, Any]) -> Collector | None:
    if kind == "greenhouse":
        return GreenhouseCollector(str(spec["company"]), str(spec["board"]))
    if kind == "lever":
        return LeverCollector(str(spec["company"]), str(spec["site"]))
    if kind == "ashby":
        return AshbyCollector(str(spec["company"]), str(spec["board"]))
    if kind == "workday-search":
        return WorkdaySearchCollector(
            str(spec["company"]),
            str(spec["host"]),
            str(spec["tenant"]),
            str(spec["site"]),
            terms=tuple(spec.get("terms") or ("intern", "co-op")),
        )
    if kind == "smartrecruiters":
        return SmartRecruitersCollector(str(spec["company"]), str(spec["identifier"]))
    if kind == "recruitee":
        return RecruiteeCollector(str(spec["company"]), str(spec["subdomain"]))
    if kind == "workable":
        return WorkableCollector(str(spec["company"]), str(spec["subdomain"]))
    if kind == "jobvite":
        return JobviteCollector(str(spec["company"]), str(spec["slug"]))
    if kind == "icims":
        return ICIMSCollector(str(spec["company"]), str(spec["host"]))
    if kind == "oracle-cloud":
        return OracleCloudCollector(
            str(spec["company"]), str(spec["origin"]), str(spec["site"])
        )
    if kind == "successfactors":
        return SuccessFactorsCollector(
            str(spec["company"]), str(spec["origin"]), str(spec["company_id"])
        )
    if kind == "domain":
        return SitemapDomainCollector(
            str(spec["company"]),
            str(spec["host"]),
            [str(url) for url in spec.get("seed_urls") or []],
        )
    if kind == "verification":
        company = str(spec["company"])
        leads = [
            Posting(
                company=company,
                title=str(item.get("title") or ""),
                apply_url=str(item.get("url") or ""),
                source="catalog:verification",
                source_id=str(item.get("source_id") or item.get("url") or ""),
                locations=[str(value) for value in item.get("locations") or []],
                source_mode="registry",
            )
            for item in spec.get("leads") or []
            if item.get("title") and item.get("url")
        ]
        return SchemaPageCollector(
            company,
            [str(url) for url in spec.get("urls") or []],
            name=canonical_source_name(str(spec.get("name") or "")) or None,
            leads=leads,
            trusted=bool(spec.get("trusted", True))
            and all(is_actionable_application_url(str(url)) for url in spec.get("urls") or []),
        )
    if kind == "google-careers":
        return GoogleInternshipCollector()
    return None


def load_catalog(database: Database) -> list[Collector]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT c.source, c.kind, c.scope, c.spec, h.lifecycle
            FROM source_catalog AS c
            LEFT JOIN source_health AS h ON h.source=c.source
            ORDER BY c.source
            """
        ).fetchall()
    merged: dict[str, Collector] = {}
    for row in rows:
        try:
            collector = _collector(str(row["kind"]), dict(row["spec"] or {}))
        except (KeyError, TypeError, ValueError):
            continue
        if collector is None:
            continue
        collector.scope = str(row["scope"])
        if str(row["lifecycle"] or "") == "quarantined":
            collector.scope = "historical"
        key = canonical_source_name(collector.name)
        existing = merged.get(key)
        if existing is None or (existing.scope == "historical" and collector.scope == "current"):
            merged[key] = collector
    return list(merged.values())


def merge_catalog(*groups: list[Collector]) -> list[Collector]:
    merged: dict[str, Collector] = {}
    for group in groups:
        for collector in group:
            key = canonical_source_name(collector.name)
            existing = merged.get(key)
            if existing is None or (
                existing.scope == "historical" and collector.scope == "current"
            ):
                merged[key] = collector
    return list(merged.values())
