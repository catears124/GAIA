from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .db import Database
from .quality import TECH_CATEGORIES, canonical_company

UNIVERSE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS employer_universe (
        employer_key TEXT PRIMARY KEY,
        canonical_name TEXT NOT NULL,
        aliases TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        resolution_status TEXT NOT NULL
            CHECK (resolution_status IN ('enumerated','located','indexed','historical','candidate')),
        evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
        evidence_types TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        evidence_sources TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        historical_years SMALLINT[] NOT NULL DEFAULT ARRAY[]::SMALLINT[],
        historical_internships INTEGER NOT NULL DEFAULT 0 CHECK (historical_internships >= 0),
        current_index_mentions INTEGER NOT NULL DEFAULT 0 CHECK (current_index_mentions >= 0),
        direct_sources INTEGER NOT NULL DEFAULT 0 CHECK (direct_sources >= 0),
        direct_openings INTEGER NOT NULL DEFAULT 0 CHECK (direct_openings >= 0),
        technical_roles INTEGER NOT NULL DEFAULT 0 CHECK (technical_roles >= 0),
        internship_probability DOUBLE PRECISION NOT NULL
            CHECK (internship_probability BETWEEN 0 AND 1),
        technical_probability DOUBLE PRECISION NOT NULL
            CHECK (technical_probability BETWEEN 0 AND 1),
        frontier_score DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (frontier_score >= 0),
        blind_spot BOOLEAN NOT NULL DEFAULT FALSE,
        first_seen_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_employer_universe_frontier
        ON employer_universe (resolution_status, frontier_score DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_employer_universe_blind_spot
        ON employer_universe (blind_spot, frontier_score DESC)
        WHERE blind_spot
    """,
    """
    CREATE TABLE IF NOT EXISTS employer_evidence (
        evidence_key TEXT PRIMARY KEY,
        employer_key TEXT NOT NULL REFERENCES employer_universe(employer_key) ON DELETE CASCADE,
        evidence_type TEXT NOT NULL,
        source TEXT NOT NULL,
        event_year SMALLINT,
        role_count INTEGER NOT NULL DEFAULT 0 CHECK (role_count >= 0),
        active_roles INTEGER NOT NULL DEFAULT 0 CHECK (active_roles >= 0),
        first_seen_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL,
        sample_url TEXT,
        metadata JSONB NOT NULL DEFAULT '{}'::JSONB
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_employer_evidence_employer
        ON employer_evidence (employer_key, evidence_type)
    """,
)


def ensure_universe_schema(database: Database) -> None:
    with database.connect() as connection:
        for statement in UNIVERSE_SCHEMA_STATEMENTS:
            connection.execute(statement)


def _employer_key(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _evidence_type(row: Mapping[str, Any]) -> str:
    mode = str(row.get("source_mode") or "")
    if mode == "direct":
        return "validated-source" if row.get("validated") and row.get("catalog_scope") == "current" else "historical-direct"
    return {
        "registry": "current-index",
        "external-index": "market-index",
        "universe-seed": "historical-internship",
        "verification": "employer-page",
        "verification-lead": "employer-page-lead",
    }.get(mode, "other-evidence")


def _event_year(row: Mapping[str, Any]) -> int | None:
    value = row.get("year")
    if value is not None:
        return int(value)
    source = str(row.get("source") or "")
    match = re.search(r"(?:19|20)\d{2}", source)
    return int(match.group(0)) if match else None


def _probabilities(*, direct_sources: int, registry: int, verification: int, historical: int, market: int, technical_roles: int) -> tuple[float, float]:
    if direct_sources:
        internship = 0.999
    else:
        internship = 0.18
        internship += 0.52 * min(1, registry)
        internship += 0.38 * min(1, verification)
        internship += 0.20 * min(3, historical)
        internship += 0.12 * min(2, market)
        internship = min(0.985, internship)
    technical = 0.08 if technical_roles == 0 else min(0.995, 0.38 + 0.24 * math.log1p(technical_roles))
    return round(internship, 4), round(technical, 4)


def rebuild_employer_universe(database: Database) -> dict[str, int]:
    """Rebuild the employer census from auditable posting evidence.

    The universe is employer based, not URL based. Historical archives, current public
    indexes, employer pages, and validated boards are evidence on the same employer node.
    An employer remains in the frontier until GAIA independently enumerates its hiring
    surface.
    """

    ensure_universe_schema(database)
    now = datetime.now(UTC)
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT posting.company, posting.source, posting.source_mode, posting.category,
                   posting.year, posting.active, posting.first_seen_at, posting.last_seen_at,
                   posting.canonical_apply_url, catalog.validated,
                   catalog.scope AS catalog_scope
            FROM postings AS posting
            LEFT JOIN source_catalog AS catalog USING(source)
            WHERE posting.target_match!='not_internship'
              AND btrim(posting.company)<>''
            """
        ).fetchall()

        employers: dict[str, dict[str, Any]] = {}
        evidence: dict[tuple[str, str, str, int | None], dict[str, Any]] = {}
        for row in rows:
            raw_name = str(row["company"]).strip()
            name = canonical_company(raw_name) or raw_name
            key = _employer_key(name)
            item = employers.setdefault(
                key,
                {
                    "name": name,
                    "aliases": set(),
                    "types": set(),
                    "sources": set(),
                    "years": set(),
                    "historical": 0,
                    "registry": 0,
                    "market": 0,
                    "verification": 0,
                    "direct_sources": set(),
                    "direct_openings": 0,
                    "technical_roles": 0,
                    "first_seen": row["first_seen_at"],
                    "last_seen": row["last_seen_at"],
                },
            )
            item["aliases"].add(raw_name)
            kind = _evidence_type(row)
            item["types"].add(kind)
            item["sources"].add(str(row["source"]))
            year = _event_year(row)
            if year:
                item["years"].add(year)
            if kind == "historical-internship":
                item["historical"] += 1
            elif kind == "current-index":
                item["registry"] += 1
            elif kind == "market-index":
                item["market"] += 1
            elif kind in {"employer-page", "employer-page-lead"}:
                item["verification"] += 1
            elif kind == "validated-source":
                item["direct_sources"].add(str(row["source"]))
                item["direct_openings"] += int(bool(row["active"]))
            if str(row["category"]) in TECH_CATEGORIES:
                item["technical_roles"] += 1
            item["first_seen"] = min(item["first_seen"], row["first_seen_at"])
            item["last_seen"] = max(item["last_seen"], row["last_seen_at"])

            evidence_key = (key, kind, str(row["source"]), year)
            ev = evidence.setdefault(
                evidence_key,
                {
                    "roles": 0,
                    "active": 0,
                    "first_seen": row["first_seen_at"],
                    "last_seen": row["last_seen_at"],
                    "sample_url": str(row["canonical_apply_url"]),
                },
            )
            ev["roles"] += 1
            ev["active"] += int(bool(row["active"]))
            ev["first_seen"] = min(ev["first_seen"], row["first_seen_at"])
            ev["last_seen"] = max(ev["last_seen"], row["last_seen_at"])

        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("gaia:employer-universe",))
        connection.execute("DELETE FROM employer_evidence")
        connection.execute("DELETE FROM employer_universe")

        universe_rows: list[tuple[Any, ...]] = []
        for key, item in employers.items():
            direct_count = len(item["direct_sources"])
            internship, technical = _probabilities(
                direct_sources=direct_count,
                registry=item["registry"],
                verification=item["verification"],
                historical=item["historical"],
                market=item["market"],
                technical_roles=item["technical_roles"],
            )
            if direct_count:
                status = "enumerated"
            elif item["verification"]:
                status = "located"
            elif item["registry"]:
                status = "indexed"
            elif item["historical"]:
                status = "historical"
            else:
                status = "candidate"
            age_days = max(0.0, (now - item["last_seen"]).total_seconds() / 86400)
            recency = math.exp(-age_days / 730)
            repeated_history = min(1.0, item["historical"] / 3)
            outside_registry = 1.0 if item["registry"] == 0 else 0.15
            frontier = 0.0 if direct_count else 100 * (
                0.42 * internship
                + 0.30 * technical
                + 0.12 * recency
                + 0.10 * repeated_history
                + 0.06 * outside_registry
            )
            blind_spot = bool(
                not direct_count
                and item["registry"] == 0
                and (item["historical"] or item["verification"] or item["market"])
                and technical >= 0.55
            )
            universe_rows.append(
                (
                    key,
                    item["name"],
                    sorted(item["aliases"]),
                    status,
                    len(item["types"]),
                    sorted(item["types"]),
                    sorted(item["sources"]),
                    sorted(item["years"]),
                    item["historical"],
                    item["registry"],
                    direct_count,
                    item["direct_openings"],
                    item["technical_roles"],
                    internship,
                    technical,
                    round(frontier, 3),
                    blind_spot,
                    item["first_seen"],
                    item["last_seen"],
                    now,
                )
            )

        if universe_rows:
            connection.executemany(
                """
                INSERT INTO employer_universe(
                    employer_key, canonical_name, aliases, resolution_status,
                    evidence_count, evidence_types, evidence_sources, historical_years,
                    historical_internships, current_index_mentions, direct_sources,
                    direct_openings, technical_roles, internship_probability,
                    technical_probability, frontier_score, blind_spot,
                    first_seen_at, last_seen_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                universe_rows,
            )

        evidence_rows: list[tuple[Any, ...]] = []
        for (employer_key, kind, source, year), item in evidence.items():
            digest = hashlib.sha256(f"{employer_key}|{kind}|{source}|{year}".encode("utf-8")).hexdigest()[:28]
            evidence_rows.append(
                (
                    digest,
                    employer_key,
                    kind,
                    source,
                    year,
                    item["roles"],
                    item["active"],
                    item["first_seen"],
                    item["last_seen"],
                    item["sample_url"],
                )
            )
        if evidence_rows:
            connection.executemany(
                """
                INSERT INTO employer_evidence(
                    evidence_key, employer_key, evidence_type, source, event_year,
                    role_count, active_roles, first_seen_at, last_seen_at, sample_url
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                evidence_rows,
            )

    return {
        "employers": len(employers),
        "evidence": len(evidence),
        "frontier": sum(not item["direct_sources"] for item in employers.values()),
    }


def universe_summary(database: Database, *, limit: int = 80) -> dict[str, object]:
    with database.connect() as connection:
        exists = connection.execute("SELECT to_regclass('employer_universe') AS name").fetchone()["name"]
        if not exists:
            return {"ready": False, "summary": {}, "frontier": []}
        counts = connection.execute(
            """
            SELECT
                COUNT(*) AS known_employers,
                COUNT(*) FILTER (WHERE resolution_status='enumerated') AS enumerated_employers,
                COUNT(*) FILTER (WHERE resolution_status!='enumerated') AS unresolved_employers,
                COUNT(*) FILTER (WHERE blind_spot) AS blind_spots,
                COUNT(*) FILTER (WHERE historical_internships>0) AS historical_employers,
                COUNT(*) FILTER (WHERE current_index_mentions>0) AS registry_employers,
                ROUND(100.0 * COUNT(*) FILTER (WHERE resolution_status='enumerated') / NULLIF(COUNT(*),0), 1)
                    AS employer_resolution_percent
            FROM employer_universe
            """
        ).fetchone()
        candidate_count = connection.execute(
            """
            SELECT COUNT(*) AS count FROM source_candidates
            WHERE status IN ('candidate','retry')
            """
        ).fetchone()["count"]
        validated_count = connection.execute(
            """
            SELECT COUNT(*) AS count FROM source_catalog
            WHERE validated AND scope='current'
            """
        ).fetchone()["count"]
        frontier = [
            dict(row)
            for row in connection.execute(
                """
                SELECT employer_key, canonical_name, resolution_status, evidence_count,
                       evidence_types, historical_years, internship_probability,
                       technical_probability, frontier_score, blind_spot, last_seen_at
                FROM employer_universe
                WHERE resolution_status!='enumerated'
                ORDER BY blind_spot DESC, frontier_score DESC, lower(canonical_name)
                LIMIT %s
                """,
                (max(1, min(limit, 250)),),
            ).fetchall()
        ]
    summary = {key: (float(value) if key == "employer_resolution_percent" and value is not None else int(value or 0)) for key, value in counts.items()}
    summary["candidate_surfaces"] = int(candidate_count or 0)
    summary["validated_sources"] = int(validated_count or 0)
    return {"ready": True, "summary": summary, "frontier": frontier}
