from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .grouping import family_key, normalize_title
from .models import CollectorResult, Posting

TARGET_MATCHES = {"exact", "year_confirmed", "source_confirmed"}
TARGET_RANK = {
    "not_internship": -1,
    "wrong_year": -1,
    "wrong_season": -1,
    "unknown": 0,
    "source_confirmed": 1,
    "year_confirmed": 2,
    "exact": 3,
}
SOURCE_RANK = {
    "direct": 0,
    "verification": 1,
    "external-index": 2,
    "registry": 3,
    "universe-seed": 4,
}
EMPLOYER_DATE_MODES = {"direct", "verification"}
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def application_identity(url: str, source: str, source_id: str) -> str:
    """Collapse copies from different feeds without merging distinct requisitions."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query)

    if gh_jid := (query.get("gh_jid") or query.get("job_id")):
        return f"greenhouse:{gh_jid[0]}"
    if "greenhouse" in host:
        if match := re.search(r"/(?:jobs?|apply)/(\d+)(?:/|$)", path):
            return f"greenhouse:{match.group(1)}"
    if host == "jobs.lever.co":
        if match := UUID_RE.search(path):
            return f"lever:{match.group(0).lower()}"
    if host == "jobs.ashbyhq.com":
        if match := UUID_RE.search(path):
            return f"ashby:{match.group(0).lower()}"
    if "google.com" in host:
        if match := re.search(r"/jobs/results/(\d+)", path):
            return f"google:{match.group(1)}"
    if "smartrecruiters.com" in host:
        if match := re.search(r"/(\d{8,})(?:/|$)", path):
            return f"smartrecruiters:{match.group(1)}"
    if "myworkdayjobs.com" in host:
        if match := re.search(r"_([A-Za-z]{0,6}\d[A-Za-z0-9-]*)$", path):
            return f"workday:{host}:{match.group(1).lower()}"

    if path.endswith("/apply"):
        path = path[: -len("/apply")]
    normalized = urlunsplit((parts.scheme.lower(), host, path or "/", parts.query, ""))
    return normalized or f"{source}:{source_id}"


def _source_sort(row: sqlite3.Row) -> tuple[int, bool, int, str]:
    return (
        SOURCE_RANK.get(str(row["source_mode"]), 99),
        row["posted_at"] is None,
        len(str(row["title"])),
        str(row["title"]),
    )


def _target_sort(row: sqlite3.Row) -> tuple[int, int]:
    return (
        TARGET_RANK.get(str(row["target_match"]), -2),
        -SOURCE_RANK.get(str(row["source_mode"]), 99),
    )


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("GAIA_DB", "data/gaia.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = float(os.getenv("GAIA_DB_TIMEOUT", "60"))
        self.migrate()

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS postings (
                    posting_key TEXT PRIMARY KEY,
                    family_key TEXT NOT NULL,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    locations_json TEXT NOT NULL,
                    apply_url TEXT NOT NULL,
                    canonical_apply_url TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    employment_type TEXT NOT NULL DEFAULT '',
                    posted_at TEXT,
                    updated_at TEXT,
                    posted_raw TEXT,
                    posted_precision TEXT NOT NULL DEFAULT 'unknown',
                    posted_confidence TEXT NOT NULL DEFAULT 'unknown',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    category TEXT NOT NULL,
                    season TEXT,
                    year INTEGER,
                    target_match TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_postings_family ON postings(family_key, active);
                CREATE INDEX IF NOT EXISTS idx_postings_target ON postings(target_match, active);
                CREATE INDEX IF NOT EXISTS idx_postings_company ON postings(company, active);

                CREATE TABLE IF NOT EXISTS families (
                    family_key TEXT PRIMARY KEY,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    season TEXT,
                    year INTEGER,
                    target_match TEXT NOT NULL,
                    opening_count INTEGER NOT NULL,
                    location_count INTEGER NOT NULL,
                    locations_json TEXT NOT NULL,
                    openings_json TEXT NOT NULL,
                    first_posted_at TEXT,
                    latest_posted_at TEXT,
                    posted_precision TEXT NOT NULL,
                    first_detected_at TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    direct_openings INTEGER NOT NULL,
                    backstop_openings INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_families_target
                    ON families(target_match, latest_posted_at);
                CREATE INDEX IF NOT EXISTS idx_families_company ON families(company);

                CREATE TABLE IF NOT EXISTS source_health (
                    source TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    complete INTEGER NOT NULL,
                    rows_scanned INTEGER NOT NULL,
                    expected_rows INTEGER,
                    target_rows INTEGER NOT NULL,
                    last_attempt_at TEXT NOT NULL,
                    last_success_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    sources INTEGER NOT NULL DEFAULT 0,
                    postings INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def start_run(self) -> int:
        now = iso(datetime.now(timezone.utc))
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO sync_runs(started_at, status) VALUES (?, 'running')", (now,)
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, sources: int, postings: int, failed: int) -> None:
        status = "ok" if failed == 0 else "partial"
        with self.connect() as db:
            db.execute(
                "UPDATE sync_runs SET finished_at=?, status=?, sources=?, postings=?, failed=? "
                "WHERE id=?",
                (iso(datetime.now(timezone.utc)), status, sources, postings, failed, run_id),
            )

    def apply_result(self, result: CollectorResult, *, rebuild: bool = True) -> None:
        observed = iso(datetime.now(timezone.utc))
        current_keys = {posting.posting_key for posting in result.postings}
        with self.connect() as db:
            old_keys = {
                str(row["posting_key"])
                for row in db.execute(
                    "SELECT posting_key FROM postings WHERE source=? AND active=1",
                    (result.source,),
                )
            }
            for posting in result.postings:
                db.execute(
                    """
                    INSERT INTO postings(
                        posting_key, family_key, company, title, normalized_title, locations_json,
                        apply_url, canonical_apply_url, source, source_id, source_mode, description,
                        employment_type, posted_at, updated_at, posted_raw, posted_precision,
                        posted_confidence, first_seen_at, last_seen_at, active, category, season,
                        year, target_match
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)
                    ON CONFLICT(posting_key) DO UPDATE SET
                        family_key=excluded.family_key,
                        company=excluded.company,
                        title=excluded.title,
                        normalized_title=excluded.normalized_title,
                        locations_json=excluded.locations_json,
                        apply_url=excluded.apply_url,
                        canonical_apply_url=excluded.canonical_apply_url,
                        source_mode=excluded.source_mode,
                        description=excluded.description,
                        employment_type=excluded.employment_type,
                        posted_at=COALESCE(excluded.posted_at, postings.posted_at),
                        updated_at=COALESCE(excluded.updated_at, postings.updated_at),
                        posted_raw=COALESCE(excluded.posted_raw, postings.posted_raw),
                        posted_precision=CASE
                            WHEN excluded.posted_at IS NOT NULL THEN excluded.posted_precision
                            ELSE postings.posted_precision END,
                        posted_confidence=CASE
                            WHEN excluded.posted_at IS NOT NULL THEN excluded.posted_confidence
                            ELSE postings.posted_confidence END,
                        last_seen_at=excluded.last_seen_at,
                        active=1,
                        category=excluded.category,
                        season=excluded.season,
                        year=excluded.year,
                        target_match=excluded.target_match
                    """,
                    (
                        posting.posting_key,
                        family_key(posting),
                        posting.company,
                        posting.title,
                        normalize_title(posting.title),
                        json.dumps(sorted(set(posting.locations))),
                        posting.apply_url,
                        posting.canonical_apply_url,
                        posting.source,
                        posting.source_id,
                        posting.source_mode,
                        posting.description,
                        posting.employment_type,
                        iso(posting.posted_at),
                        iso(posting.updated_at),
                        posting.posted_raw,
                        posting.posted_precision,
                        posting.posted_confidence,
                        observed,
                        observed,
                        posting.category,
                        posting.season,
                        posting.year,
                        posting.target_match,
                    ),
                )

            if result.complete:
                missing = old_keys - current_keys
                if missing:
                    placeholders = ",".join("?" for _ in missing)
                    db.execute(
                        f"UPDATE postings SET active=0 WHERE posting_key IN ({placeholders})",
                        tuple(sorted(missing)),
                    )

            target_rows = sum(posting.target_match in TARGET_MATCHES for posting in result.postings)
            db.execute(
                """
                INSERT INTO source_health(source, mode, complete, rows_scanned, expected_rows,
                                          target_rows, last_attempt_at, last_success_at, last_error)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                    mode=excluded.mode,
                    complete=excluded.complete,
                    rows_scanned=excluded.rows_scanned,
                    expected_rows=excluded.expected_rows,
                    target_rows=excluded.target_rows,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at,
                    last_error=excluded.last_error
                """,
                (
                    result.source,
                    result.mode,
                    int(result.complete),
                    result.rows_scanned,
                    result.expected_rows,
                    target_rows,
                    observed,
                    observed if result.error is None else None,
                    result.error,
                ),
            )
        if rebuild:
            self.rebuild_families()

    def record_failure(self, result: CollectorResult) -> None:
        now = iso(datetime.now(timezone.utc))
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO source_health(source, mode, complete, rows_scanned, expected_rows,
                                          target_rows, last_attempt_at, last_success_at, last_error)
                VALUES (?,?,?,?,?,0,?,NULL,?)
                ON CONFLICT(source) DO UPDATE SET
                    mode=excluded.mode,
                    complete=0,
                    rows_scanned=excluded.rows_scanned,
                    expected_rows=excluded.expected_rows,
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error
                """,
                (
                    result.source,
                    result.mode,
                    0,
                    result.rows_scanned,
                    result.expected_rows,
                    now,
                    result.error,
                ),
            )

    def rebuild_families(self) -> None:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM postings WHERE active=1").fetchall()
            db.execute("DELETE FROM families")

            variants_by_application: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                identity = application_identity(
                    str(row["canonical_apply_url"]),
                    str(row["source"]),
                    str(row["source_id"]),
                )
                variants_by_application.setdefault(identity, []).append(row)

            applications_by_family: dict[str, list[dict[str, object]]] = {}
            for identity, variants in variants_by_application.items():
                selected = min(variants, key=_source_sort)
                target_anchor = max(variants, key=_target_sort)
                canonical_family = str(target_anchor["family_key"])
                locations = sorted(
                    {
                        location
                        for row in variants
                        for location in json.loads(str(row["locations_json"]))
                        if location
                    }
                )
                employer_date_rows = [
                    row
                    for row in variants
                    if row["posted_at"]
                    and str(row["source_mode"]) in EMPLOYER_DATE_MODES
                ]
                employer_dates = sorted(str(row["posted_at"]) for row in employer_date_rows)
                has_direct = any(str(row["source_mode"]) == "direct" for row in variants)
                application = {
                    "identity": identity,
                    "selected": selected,
                    "target_anchor": target_anchor,
                    "variants": variants,
                    "locations": locations,
                    "employer_dates": employer_dates,
                    "employer_precisions": [
                        str(row["posted_precision"]) for row in employer_date_rows
                    ],
                    "has_direct": has_direct,
                    "opening": {
                        "application_identity": identity,
                        "posting_key": selected["posting_key"],
                        "location": locations,
                        "apply_url": selected["apply_url"],
                        "source": selected["source"],
                        "source_mode": selected["source_mode"],
                        "posted_at": employer_dates[0] if employer_dates else None,
                        "source_variants": sorted(
                            {
                                f"{row['source_mode']}:{row['source']}"
                                for row in variants
                            }
                        ),
                    },
                }
                applications_by_family.setdefault(canonical_family, []).append(application)

            for key, applications in applications_by_family.items():
                selected_rows = [app["selected"] for app in applications]
                preferred = min(selected_rows, key=_source_sort)
                anchors = [app["target_anchor"] for app in applications]
                target_anchor = max(anchors, key=_target_sort)
                target = str(target_anchor["target_match"])
                locations = sorted(
                    {
                        location
                        for application in applications
                        for location in application["locations"]
                    }
                )
                openings = [application["opening"] for application in applications]
                openings.sort(key=lambda item: (item["location"], item["apply_url"]))
                employer_dates = sorted(
                    date
                    for application in applications
                    for date in application["employer_dates"]
                )
                precisions = [
                    precision
                    for application in applications
                    for precision in application["employer_precisions"]
                ]
                precision = "timestamp" if "timestamp" in precisions else (
                    "day" if "day" in precisions else "unknown"
                )
                variant_rows = [
                    row
                    for application in applications
                    for row in application["variants"]
                ]
                first_seen = min(str(row["first_seen_at"]) for row in variant_rows)
                last_seen = max(str(row["last_seen_at"]) for row in variant_rows)
                direct_openings = sum(bool(app["has_direct"]) for app in applications)
                backstop_openings = len(applications) - direct_openings

                db.execute(
                    "INSERT INTO families VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        preferred["company"],
                        preferred["title"],
                        preferred["category"],
                        target_anchor["season"],
                        target_anchor["year"],
                        target,
                        len(openings),
                        len(locations),
                        json.dumps(locations),
                        json.dumps(openings),
                        employer_dates[0] if employer_dates else None,
                        employer_dates[-1] if employer_dates else None,
                        precision,
                        first_seen,
                        last_seen,
                        direct_openings,
                        backstop_openings,
                    ),
                )

    def list_families(
        self,
        *,
        query: str = "",
        category: str = "",
        target: str = "default",
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, object]:
        conditions: list[str] = []
        params: list[object] = []
        if target == "default":
            conditions.append("target_match IN ('exact','year_confirmed','source_confirmed')")
        elif target:
            conditions.append("target_match=?")
            params.append(target)
        if category:
            conditions.append("category=?")
            params.append(category)
        if query:
            conditions.append("(company LIKE ? OR title LIKE ? OR locations_json LIKE ?)")
            needle = f"%{query}%"
            params.extend([needle, needle, needle])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        offset = max(0, page - 1) * page_size
        with self.connect() as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM families{where}", params).fetchone()[0])
            rows = db.execute(
                f"""SELECT * FROM families{where}
                    ORDER BY COALESCE(latest_posted_at, first_detected_at) DESC
                    LIMIT ? OFFSET ?""",
                [*params, page_size, offset],
            ).fetchall()
        return {"total": total, "items": [self._family_dict(row) for row in rows]}

    def get_family(self, key: str) -> dict[str, object] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM families WHERE family_key=?", (key,)).fetchone()
        return self._family_dict(row) if row else None

    def coverage(self) -> dict[str, object]:
        with self.connect() as db:
            health = [dict(row) for row in db.execute("SELECT * FROM source_health ORDER BY source")]
            family_counts = dict(
                db.execute(
                    """
                    SELECT
                        COUNT(*) AS families,
                        COUNT(DISTINCT company) AS companies,
                        COALESCE(SUM(direct_openings > 0), 0) AS direct_families,
                        COALESCE(SUM(direct_openings = 0 AND backstop_openings > 0), 0)
                            AS backstop_only
                    FROM families
                    WHERE target_match IN ('exact','year_confirmed','source_confirmed')
                    """
                ).fetchone()
            )
            posting_rows = db.execute(
                """
                SELECT canonical_apply_url, source, source_id, source_mode, company
                FROM postings
                WHERE active=1
                  AND target_match IN ('exact','year_confirmed','source_confirmed')
                """
            ).fetchall()

        identities: dict[str, set[str]] = {
            "registry": set(),
            "direct": set(),
            "verification": set(),
            "external-index": set(),
        }
        companies_by_mode = {mode: set() for mode in identities}
        for row in posting_rows:
            mode = str(row["source_mode"])
            if mode not in identities:
                continue
            identity = application_identity(
                str(row["canonical_apply_url"]),
                str(row["source"]),
                str(row["source_id"]),
            )
            identities[mode].add(identity)
            companies_by_mode[mode].add(str(row["company"]))

        independently_recovered = identities["direct"] | identities["verification"]
        registry_floor = identities["registry"]
        direct_matches = registry_floor & identities["direct"]
        independent_matches = registry_floor & independently_recovered
        registry_only = registry_floor - independently_recovered
        direct_only = identities["direct"] - registry_floor
        mode_counts = Counter(str(row["mode"]) for row in health)
        complete_enumerators = sum(
            bool(row["complete"]) and str(row["mode"]) == "board" for row in health
        )
        broken = sum(bool(row["last_error"]) for row in health)
        zero_result_enumerators = sum(
            bool(row["complete"])
            and str(row["mode"]) == "board"
            and int(row["rows_scanned"] or 0) == 0
            for row in health
        )
        truncated = sum(
            row["expected_rows"] is not None
            and int(row["rows_scanned"] or 0) < int(row["expected_rows"])
            for row in health
        )
        registry_recall = (
            round(100 * len(independent_matches) / len(registry_floor), 1)
            if registry_floor
            else None
        )

        return {
            "summary": {
                **family_counts,
                "known_applications": len(set().union(*identities.values())),
                "registry_floor": len(registry_floor),
                "direct_applications": len(identities["direct"]),
                "verified_applications": len(identities["verification"]),
                "direct_matches": len(direct_matches),
                "independent_matches": len(independent_matches),
                "registry_only": len(registry_only),
                "direct_only": len(direct_only),
                "registry_recall_percent": registry_recall,
            },
            "contract": {
                "configured_sources": len(health),
                "complete_enumerators": complete_enumerators,
                "broken_sources": broken,
                "zero_result_enumerators": zero_result_enumerators,
                "truncated_sources": truncated,
                "modes": dict(mode_counts),
                "companies_by_mode": {
                    mode: len(companies) for mode, companies in companies_by_mode.items()
                },
            },
            "sources": health,
        }

    @staticmethod
    def _family_dict(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        result["locations"] = json.loads(result.pop("locations_json"))
        result["openings"] = json.loads(result.pop("openings_json"))
        return result
