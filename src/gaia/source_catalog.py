from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .collectors import (
    AshbyCollector,
    Collector,
    GreenhouseCollector,
    LeverCollector,
    SchemaPageCollector,
)
from .market_collectors import SitemapDomainCollector, WorkdaySearchCollector
from .native_collectors import GoogleInternshipCollector
from .provider_collectors import RecruiteeCollector, SmartRecruitersCollector, WorkableCollector
from .quality import canonical_source_name


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_catalog (
            source TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            first_discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return connection


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
            "name": canonical_source_name(collector.name),
        }
    if isinstance(collector, GoogleInternshipCollector):
        return "google-careers", {}
    return None


def save_catalog(path: Path, collectors: list[Collector]) -> int:
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
                json.dumps(spec, sort_keys=True),
            )
        )
    with _connect(path) as database:
        database.executemany(
            """
            INSERT INTO source_catalog(source, kind, scope, spec_json)
            VALUES (?,?,?,?)
            ON CONFLICT(source) DO UPDATE SET
                kind=excluded.kind,
                scope=CASE
                    WHEN source_catalog.scope='current' THEN 'current'
                    ELSE excluded.scope
                END,
                spec_json=excluded.spec_json,
                last_discovered_at=CURRENT_TIMESTAMP
            """,
            rows,
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
    if kind == "domain":
        return SitemapDomainCollector(
            str(spec["company"]),
            str(spec["host"]),
            [str(url) for url in spec.get("seed_urls") or []],
        )
    if kind == "verification":
        return SchemaPageCollector(
            str(spec["company"]),
            [str(url) for url in spec.get("urls") or []],
            name=canonical_source_name(str(spec.get("name") or "")) or None,
        )
    if kind == "google-careers":
        return GoogleInternshipCollector()
    return None


def load_catalog(path: Path) -> list[Collector]:
    with _connect(path) as database:
        rows = database.execute(
            "SELECT source, kind, scope, spec_json FROM source_catalog ORDER BY source"
        ).fetchall()
    merged: dict[str, Collector] = {}
    for row in rows:
        try:
            collector = _collector(str(row["kind"]), json.loads(str(row["spec_json"])))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if collector is None:
            continue
        collector.scope = str(row["scope"])
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
