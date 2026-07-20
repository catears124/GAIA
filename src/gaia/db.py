from __future__ import annotations

import json
import os
import re
import sqlite3
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
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def application_identity(url: str, source: str, source_id: str) -> str:
    """Collapse direct and fallback copies without merging distinct requisitions."""
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
    if source.startswith("workday:") and source_id:
        return f"{source}:{source_id}"

    if path.endswith("/apply"):
        path = path[: -len("/apply")]
    normalized = urlunsplit((parts.scheme.lower(), host, path or "/", parts.query, ""))
    return normalized or f"{source}:{source_id}"


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

    def apply_result(self, result: CollectorResult) -> None:
        observed = iso(datetime.now(timezone.utc))
        current_keys = {posting.posting_key for posting in result.postings}
        impacted: set[str] = set()
        with self.connect() as db:
            old_rows = db.execute(
                "SELECT posting_key, family_key FROM postings WHERE source=? AND active=1",
                (result.source,),
            ).fetchall()
            old_keys = {str(row["posting_key"]): str(row["family_key"]) for row in old_rows}

            for posting in result.postings:
                key = family_key(posting)
                impacted.add(key)
                if old_family := old_keys.get(posting.posting_key):
                    impacted.add(old_family)
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
                        key,
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
                missing = set(old_keys) - current_keys
                if missing:
                    placeholders = ",".join("?" for _ in missing)
                    db.execute(
                        f"UPDATE postings SET active=0 WHERE posting_key IN ({placeholders})",
                        tuple(sorted(missing)),
                    )
                    impacted.update(old_keys[key] for key in missing)

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
        self.rebuild_families(impacted)

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

    def rebuild_families(self, keys: set[str] | None = None) -> None:
        with self.connect() as db:
            if keys is None:
                rows = db.execute("SELECT * FROM postings WHERE active=1").fetchall()
                db.execute("DELETE FROM families")
            elif not keys:
                return
            else:
                placeholders = ",".join("?" for _ in keys)
                rows = db.execute(
                    f"SELECT * FROM postings WHERE active=1 "
                    f"AND family_key IN ({placeholders})",
                    tuple(sorted(keys)),
                ).fetchall()
                db.execute(
                    f"DELETE FROM families WHERE family_key IN ({placeholders})",
                    tuple(sorted(keys)),
                )

            grouped: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                grouped.setdefault(str(row["family_key"]), []).append(row)

            for key, family_rows in grouped.items():
                application_rows: dict[str, list[sqlite3.Row]] = {}
                for row in family_rows:
                    identity = application_identity(
                        str(row["canonical_apply_url"]),
                        str(row["source"]),
                        str(row["source_id"]),
                    )
                    application_rows.setdefault(identity, []).append(row)

                openings: list[dict[str, object]] = []
                locations: set[str] = set()
                employer_dates: list[str] = []
                employer_precisions: list[str] = []
                direct_openings = 0
                backstop_openings = 0

                for identity, variants in application_rows.items():
                    selected = min(
                        variants,
                        key=lambda row: (
                            SOURCE_RANK.get(str(row["source_mode"]), 99),
                            row["posted_at"] is None,
                            len(str(row["title"])),
                        ),
                    )
                    opening_locations = sorted(
                        {
                            location
                            for row in variants
                            for location in json.loads(str(row["locations_json"]))
                            if location
                        }
                    )
                    locations.update(opening_locations)
                    has_direct = any(str(row["source_mode"]) == "direct" for row in variants)
                    direct_openings += int(has_direct)
                    backstop_openings += int(not has_direct)
                    dates = sorted(
                        str(row["posted_at"])
                        for row in variants
                        if row["posted_at"]
                        and str(row["source_mode"]) in EMPLOYER_DATE_MODES
                    )
                    if dates:
                        employer_dates.extend(dates)
                        employer_precisions.extend(
                            str(row["posted_precision"])
                            for row in variants
                            if row["posted_at"]
                            and str(row["source_mode"]) in EMPLOYER_DATE_MODES
                        )
                    openings.append(
                        {
                            "application_identity": identity,
                            "posting_key": selected["posting_key"],
                            "location": opening_locations,
                            "apply_url": selected["apply_url"],
                            "source": selected["source"],
                            "source_mode": selected["source_mode"],
                            "posted_at": dates[0] if dates else None,
                            "source_variants": sorted(
                                {
                                    f"{row['source_mode']}:{row['source']}"
                                    for row in variants
                                }
                            ),
                        }
                    )

                preferred_pool = [
                    row for row in family_rows if str(row["source_mode"]) == "direct"
                ] or [
                    row for row in family_rows if str(row["source_mode"]) == "verification"
                ] or family_rows
                preferred = min(
                    preferred_pool,
                    key=lambda row: (len(str(row["title"])), str(row["title"])),
                )
                target = max(
                    (str(row["target_match"]) for row in family_rows),
                    key=lambda value: TARGET_RANK.get(value, -2),
                )
                precision = "unknown"
                if "timestamp" in employer_precisions:
                    precision = "timestamp"
                elif "day" in employer_precisions:
                    precision = "day"
                first_seen = min(str(row["first_seen_at"]) for row in family_rows)
                last_seen = max(str(row["last_seen_at"]) for row in family_rows)
                employer_dates.sort()
                openings.sort(key=lambda item: (item["location"], item["apply_url"]))

                db.execute(
                    "INSERT INTO families VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        preferred["company"],
                        preferred["title"],
                        preferred["category"],
                        preferred["season"],
                        preferred["year"],
                        target,
                        len(openings),
                        len(locations),
                        json.dumps(sorted(locations)),
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
            counts = dict(
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
        complete = sum(bool(row["complete"]) for row in health)
        return {
            "summary": counts,
            "sources": health,
            "healthy": complete,
            "configured": len(health),
        }

    @staticmethod
    def _family_dict(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        result["locations"] = json.loads(result.pop("locations_json"))
        result["openings"] = json.loads(result.pop("openings_json"))
        return result
