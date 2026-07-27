from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .grouping import family_key, normalize_title
from .models import CollectorResult, canonical_url
from .quality import (
    TECH_CATEGORIES,
    canonical_company,
    is_actionable_application_url,
    normalize_locations,
)

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
    "verification-lead": 2,
    "external-index": 2,
    "registry": 3,
    "universe-seed": 4,
}
EMPLOYER_DATE_MODES = {"direct", "verification"}
INDEPENDENT_MODES = {"direct", "verification"}
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


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
    if host.endswith("amazon.jobs"):
        if match := re.search(r"/jobs/(\d+)(?:/|$)", path):
            return f"amazon:{match.group(1)}"
    if "myworkdayjobs.com" in host:
        if match := re.search(r"_([A-Za-z]{0,6}\d[A-Za-z0-9-]*)$", path):
            return f"workday:{host}:{match.group(1).lower()}"

    if path.endswith("/apply"):
        path = path[: -len("/apply")]
    normalized = urlunsplit((parts.scheme.lower(), host, path or "/", parts.query, ""))
    return normalized or f"{source}:{source_id}"

def coverage_role_signature(company: str, title: str) -> str:
    """Match benchmark roles across employer URL and punctuation changes."""
    aliases = {
        "engineering": "engineer",
        "internship": "intern",
        "internships": "intern",
    }
    ignored = {"or", "summer", "2027"}
    tokens = [
        aliases.get(token, token)
        for token in re.findall(r"[a-z0-9+#]+", title.lower())
        if token not in ignored
    ]
    return f"{canonical_company(company).casefold()}:{' '.join(tokens)}"




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
                    target_match TEXT NOT NULL,
                    link_checked_at TEXT,
                    link_http_status INTEGER,
                    link_final_url TEXT,
                    link_status TEXT NOT NULL DEFAULT 'unchecked'
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
                CREATE INDEX IF NOT EXISTS idx_families_feed
                    ON families(target_match, category, direct_openings, latest_posted_at DESC);

                CREATE TABLE IF NOT EXISTS source_health (
                    source TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    complete INTEGER NOT NULL,
                    rows_scanned INTEGER NOT NULL,
                    expected_rows INTEGER,
                    target_rows INTEGER NOT NULL,
                    last_attempt_at TEXT NOT NULL,
                    last_success_at TEXT,
                    last_error TEXT,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    scope TEXT NOT NULL DEFAULT 'current',
                    note TEXT,
                    last_run_id INTEGER,
                    lifecycle TEXT NOT NULL DEFAULT 'candidate',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS benchmark_cases (
                    version TEXT NOT NULL,
                    posting_key TEXT NOT NULL,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    employment_type TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    expected_category TEXT NOT NULL,
                    expected_target_match TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(version, posting_key)
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
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(source_health)").fetchall()
            }
            additions = {
                "status": "TEXT NOT NULL DEFAULT 'unknown'",
                "scope": "TEXT NOT NULL DEFAULT 'current'",
                "note": "TEXT",
                "last_run_id": "INTEGER",
                "lifecycle": "TEXT NOT NULL DEFAULT 'candidate'",
                "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE source_health ADD COLUMN {name} {declaration}")
            posting_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(postings)").fetchall()
            }
            posting_additions = {
                "link_checked_at": "TEXT",
                "link_http_status": "INTEGER",
                "link_final_url": "TEXT",
                "link_status": "TEXT NOT NULL DEFAULT 'unchecked'",
            }
            for name, declaration in posting_additions.items():
                if name not in posting_columns:
                    db.execute(f"ALTER TABLE postings ADD COLUMN {name} {declaration}")
            db.execute(
                """
                UPDATE source_health
                SET lifecycle='productive', consecutive_failures=0
                WHERE lifecycle='candidate'
                  AND target_rows>0
                  AND status NOT IN ('broken','blocked')
                """
            )
            db.execute(
                """
                UPDATE postings
                SET description=''
                WHERE description!=''
                  AND target_match NOT IN ('exact','year_confirmed','source_confirmed')
                """
            )

    def start_run(self) -> int:
        now = datetime.now(UTC)
        stale_after = max(300, int(os.getenv("GAIA_SYNC_LOCK_TIMEOUT", "7200")))
        cutoff = iso(now - timedelta(seconds=stale_after))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE sync_runs
                SET finished_at=?, status='cancelled'
                WHERE status='running' AND started_at<?
                """,
                (iso(now), cutoff),
            )
            running = db.execute(
                "SELECT id FROM sync_runs WHERE status='running' LIMIT 1"
            ).fetchone()
            if running is not None:
                raise RuntimeError(f"sync run {int(running['id'])} is already running")
            cursor = db.execute(
                "INSERT INTO sync_runs(started_at, status) VALUES (?, 'running')",
                (iso(now),),
            )
            return int(cursor.lastrowid)

    def seed_benchmark_corpus(self, *, version: str = "v1", limit: int = 500) -> int:
        """Freeze a deterministic, production-derived classification regression corpus."""
        if limit <= 0:
            return 0
        captured_at = iso(datetime.now(UTC))
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT posting_key, company, title, employment_type, source_mode,
                       category, target_match
                FROM postings
                WHERE active=1
                ORDER BY
                    CASE source_mode WHEN 'direct' THEN 0 WHEN 'registry' THEN 1 ELSE 2 END,
                    target_match,
                    category,
                    company COLLATE NOCASE,
                    title COLLATE NOCASE,
                    posting_key
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            db.executemany(
                """
                INSERT OR IGNORE INTO benchmark_cases(
                    version, posting_key, company, title, employment_type, source_mode,
                    expected_category, expected_target_match, captured_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        version,
                        row["posting_key"],
                        row["company"],
                        row["title"],
                        row["employment_type"],
                        row["source_mode"],
                        row["category"],
                        row["target_match"],
                        captured_at,
                    )
                    for row in rows
                ],
            )
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM benchmark_cases WHERE version=?", (version,)
                ).fetchone()[0]
            )

    def finish_run(self, run_id: int, *, sources: int, postings: int, failed: int) -> None:
        status = "ok" if failed == 0 else "partial"
        with self.connect() as db:
            db.execute(
                "UPDATE sync_runs SET finished_at=?, status=?, sources=?, postings=?, failed=? "
                "WHERE id=?",
                (iso(datetime.now(UTC)), status, sources, postings, failed, run_id),
            )

    def apply_result(
        self,
        result: CollectorResult,
        *,
        rebuild: bool = True,
        run_id: int | None = None,
    ) -> None:
        observed = iso(datetime.now(UTC))
        postings = [
            posting
            for posting in result.postings
            if posting.company.strip()
            and posting.title.strip()
            and posting.canonical_apply_url.strip()
        ]
        for posting in postings:
            posting.locations = normalize_locations(posting.locations)
        current_keys = {posting.posting_key for posting in postings}
        with self.connect() as db:
            old_keys = {
                str(row["posting_key"])
                for row in db.execute(
                    "SELECT posting_key FROM postings WHERE source=? AND active=1",
                    (result.source,),
                )
            }
            previous_target_identities: set[str] = set()
            if result.complete and result.mode in {"board", "board-search"}:
                previous_target_identities = {
                    application_identity(
                        str(row["canonical_apply_url"]),
                        str(row["source"]),
                        str(row["source_id"]),
                    )
                    for row in db.execute(
                        """
                        SELECT canonical_apply_url, source, source_id
                        FROM postings
                        WHERE source=? AND target_match IN ('exact','year_confirmed','source_confirmed')
                        """,
                        (result.source,),
                    )
                }
            for posting in postings:
                stored_description = (
                    posting.description if posting.target_match in TARGET_MATCHES else ""
                )
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
                        stored_description,
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

                current_target_identities = {
                    application_identity(
                        posting.canonical_apply_url,
                        posting.source,
                        posting.source_id,
                    )
                    for posting in postings
                    if posting.target_match in TARGET_MATCHES
                }
                if result.source.startswith("greenhouse:") and result.status in {"ok", "empty"}:
                    board = result.source.partition(":")[2].lower()
                    provider_stale_keys = [
                        str(row["posting_key"])
                        for row in db.execute(
                            """
                            SELECT posting_key, company, canonical_apply_url, source, source_id
                            FROM postings
                            WHERE active=1
                              AND source_mode IN ('registry','external-index','verification-lead')
                            """
                        )
                        if re.sub(r"[^a-z0-9]", "", str(row["company"]).lower()) == board
                        and application_identity(
                            str(row["canonical_apply_url"]),
                            str(row["source"]),
                            str(row["source_id"]),
                        )
                        not in current_target_identities
                    ]
                    if provider_stale_keys:
                        placeholders = ",".join("?" for _ in provider_stale_keys)
                        db.execute(
                            f"UPDATE postings SET active=0 WHERE posting_key IN ({placeholders})",
                            tuple(provider_stale_keys),
                        )

                stale_identities = previous_target_identities - current_target_identities
                if stale_identities:
                    stale_keys = [
                        str(row["posting_key"])
                        for row in db.execute(
                            """
                            SELECT posting_key, canonical_apply_url, source, source_id
                            FROM postings
                            WHERE active=1
                              AND source_mode IN (
                                  'registry',
                                  'external-index',
                                  'verification-lead'
                              )
                              AND target_match IN (
                                  'exact',
                                  'year_confirmed',
                                  'source_confirmed'
                              )
                            """
                        )
                        if application_identity(
                            str(row["canonical_apply_url"]),
                            str(row["source"]),
                            str(row["source_id"]),
                        )
                        in stale_identities
                    ]
                    if stale_keys:
                        placeholders = ",".join("?" for _ in stale_keys)
                        db.execute(
                            f"UPDATE postings SET active=0 WHERE posting_key IN ({placeholders})",
                            tuple(stale_keys),
                        )

            if result.closed_urls:
                closed = sorted({canonical_url(url) for url in result.closed_urls})
                placeholders = ",".join("?" for _ in closed)
                db.execute(
                    f"""UPDATE postings SET active=0
                        WHERE canonical_apply_url IN ({placeholders})
                          AND source_mode IN ('registry','verification','external-index')""",
                    tuple(closed),
                )

            target_rows = sum(posting.target_match in TARGET_MATCHES for posting in result.postings)
            last_success = (
                observed
                if result.error is None and result.status not in {"blocked", "broken"}
                else None
            )
            productive = any(
                posting.source_mode in {"direct", "verification"}
                and posting.target_match in TARGET_MATCHES
                for posting in postings
            )
            db.execute(
                """
                INSERT INTO source_health(
                    source, mode, complete, rows_scanned, expected_rows, target_rows,
                    last_attempt_at, last_success_at, last_error, status, scope, note, last_run_id,
                    lifecycle, consecutive_failures
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(source) DO UPDATE SET
                    mode=excluded.mode,
                    complete=excluded.complete,
                    rows_scanned=excluded.rows_scanned,
                    expected_rows=excluded.expected_rows,
                    target_rows=excluded.target_rows,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=COALESCE(excluded.last_success_at, source_health.last_success_at),
                    last_error=excluded.last_error,
                    status=excluded.status,
                    scope=CASE WHEN excluded.lifecycle='productive' THEN 'current' ELSE excluded.scope END,
                    note=excluded.note,
                    last_run_id=excluded.last_run_id,
                    lifecycle=CASE
                        WHEN excluded.lifecycle='productive' THEN 'productive'
                        WHEN source_health.lifecycle='quarantined' THEN 'candidate'
                        ELSE source_health.lifecycle
                    END,
                    consecutive_failures=0
                """,
                (
                    result.source,
                    result.mode,
                    int(result.complete),
                    result.rows_scanned,
                    result.expected_rows,
                    target_rows,
                    observed,
                    last_success,
                    result.error,
                    result.status,
                    result.scope,
                    result.note,
                    run_id,
                    "productive" if productive else "candidate",
                ),
            )
        if rebuild:
            self.rebuild_families()

    def record_failure(self, result: CollectorResult, *, run_id: int | None = None) -> None:
        now = iso(datetime.now(UTC))
        quarantine_after = max(1, int(os.getenv("GAIA_SOURCE_QUARANTINE_FAILURES", "3")))
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO source_health(
                    source, mode, complete, rows_scanned, expected_rows, target_rows,
                    last_attempt_at, last_success_at, last_error, status, scope, note, last_run_id,
                    lifecycle, consecutive_failures
                ) VALUES (?,?,?,?,?,0,?,NULL,?,?,?,?,?,'candidate',1)
                ON CONFLICT(source) DO UPDATE SET
                    mode=excluded.mode,
                    complete=0,
                    rows_scanned=excluded.rows_scanned,
                    expected_rows=excluded.expected_rows,
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error,
                    status=excluded.status,
                    scope=CASE
                        WHEN source_health.consecutive_failures + 1 >= ? THEN 'historical'
                        ELSE excluded.scope
                    END,
                    note=excluded.note,
                    last_run_id=excluded.last_run_id,
                    lifecycle=CASE
                        WHEN source_health.consecutive_failures + 1 >= ? THEN 'quarantined'
                        ELSE source_health.lifecycle
                    END,
                    consecutive_failures=source_health.consecutive_failures + 1
                """,
                (
                    result.source,
                    result.mode,
                    0,
                    result.rows_scanned,
                    result.expected_rows,
                    now,
                    result.error,
                    result.status,
                    result.scope,
                    result.note,
                    run_id,
                    quarantine_after,
                    quarantine_after,
                ),
            )

    def rebuild_families(self) -> None:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM postings WHERE active=1 AND target_match!='not_internship'"
            ).fetchall()
            blocked_keys = [
                str(row["posting_key"])
                for row in rows
                if not is_actionable_application_url(str(row["canonical_apply_url"]))
            ]
            if blocked_keys:
                placeholders = ",".join("?" for _ in blocked_keys)
                db.execute(
                    f"UPDATE postings SET active=0 WHERE posting_key IN ({placeholders})",
                    tuple(blocked_keys),
                )
                blocked = set(blocked_keys)
                rows = [row for row in rows if str(row["posting_key"]) not in blocked]
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
                if all(str(row["source_mode"]) == "verification-lead" for row in variants):
                    continue
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
                    if row["posted_at"] and str(row["source_mode"]) in EMPLOYER_DATE_MODES
                ]
                employer_dates = sorted(str(row["posted_at"]) for row in employer_date_rows)
                independently_recovered = any(
                    str(row["source_mode"]) == "direct" for row in variants
                )
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
                    "independently_recovered": independently_recovered,
                    "opening": {
                        "application_identity": identity,
                        "posting_key": selected["posting_key"],
                        "location": locations,
                        "apply_url": selected["apply_url"],
                        "source": selected["source"],
                        "source_mode": selected["source_mode"],
                        "posted_at": employer_dates[0] if employer_dates else None,
                        "first_detected_at": min(
                            str(row["first_seen_at"]) for row in variants if row["first_seen_at"]
                        ),
                        "last_verified_at": max(
                            str(row["last_seen_at"]) for row in variants if row["last_seen_at"]
                        ),
                        "source_variants": sorted(
                            {f"{row['source_mode']}:{row['source']}" for row in variants}
                        ),
                    },
                }
                applications_by_family.setdefault(canonical_family, []).append(application)

            def family_rows() -> Iterable[tuple[object, ...]]:
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
                    precision = (
                        "timestamp"
                        if "timestamp" in precisions
                        else ("day" if "day" in precisions else "unknown")
                    )
                    variant_rows = [
                        row for application in applications for row in application["variants"]
                    ]
                    first_seen = min(str(row["first_seen_at"]) for row in variant_rows)
                    last_seen = max(str(row["last_seen_at"]) for row in variant_rows)
                    independent_openings = sum(
                        bool(app["independently_recovered"]) for app in applications
                    )
                    yield (
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
                        independent_openings,
                        len(applications) - independent_openings,
                    )

            db.executemany(
                "INSERT INTO families VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                family_rows(),
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
            tech_placeholders = ",".join("?" for _ in TECH_CATEGORIES)
            latest_run = db.execute(
                "SELECT MAX(id) FROM sync_runs WHERE finished_at IS NOT NULL"
            ).fetchone()[0]
            if latest_run is None:
                latest_run = db.execute("SELECT MAX(last_run_id) FROM source_health").fetchone()[0]
            if latest_run is None:
                health = [dict(row) for row in db.execute("SELECT * FROM source_health ORDER BY source")]
            else:
                health = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM source_health WHERE last_run_id=? ORDER BY source",
                        (latest_run,),
                    )
                ]
            benchmark_size = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM benchmark_cases
                    WHERE version=(SELECT MAX(version) FROM benchmark_cases)
                    """
                ).fetchone()[0]
            )
            family_counts = dict(
                db.execute(
                    f"""
                    SELECT
                        COUNT(*) AS families,
                        COUNT(DISTINCT company) AS companies,
                        COALESCE(SUM(direct_openings > 0), 0) AS direct_families,
                        COALESCE(SUM(direct_openings = 0 AND backstop_openings > 0), 0)
                            AS backstop_only
                    FROM families
                    WHERE target_match IN ('exact','year_confirmed','source_confirmed')
                      AND category IN ({tech_placeholders})
                    """,
                    TECH_CATEGORIES,
                ).fetchone()
            )
            posting_rows = db.execute(
                f"""
                SELECT canonical_apply_url, source, source_id, source_mode, company,
                       normalized_title, posted_at
                FROM postings
                WHERE active=1
                  AND target_match IN ('exact','year_confirmed','source_confirmed')
                  AND category IN ({tech_placeholders})
                """,
                TECH_CATEGORIES,
            ).fetchall()

        identities: dict[str, set[str]] = {
            "registry": set(),
            "direct": set(),
            "verification": set(),
            "external-index": set(),
        }
        identity_roles: dict[str, dict[str, set[str]]] = {mode: {} for mode in identities}
        companies_by_mode = {mode: set() for mode in identities}
        productive_direct_sources: set[str] = set()
        dated_direct_applications: set[str] = set()
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
            if mode == "direct":
                productive_direct_sources.add(str(row["source"]))
                if row["posted_at"]:
                    dated_direct_applications.add(identity)
            role = coverage_role_signature(str(row["company"]), str(row["normalized_title"]))
            identity_roles[mode].setdefault(identity, set()).add(role)
            companies_by_mode[mode].add(str(row["company"]))

        registry_floor = identities["registry"]
        direct_roles = set().union(*identity_roles["direct"].values())
        independent_roles = direct_roles | set().union(*identity_roles["verification"].values())
        direct_matches = {
            identity
            for identity in registry_floor
            if identity in identities["direct"]
            or bool(identity_roles["registry"][identity] & direct_roles)
        }
        independent_matches = {
            identity
            for identity in registry_floor
            if identity in identities["direct"] | identities["verification"]
            or bool(identity_roles["registry"][identity] & independent_roles)
        }
        registry_only = registry_floor - independent_matches
        registry_roles = set().union(*identity_roles["registry"].values())
        direct_only = {
            identity
            for identity in identities["direct"]
            if not identity_roles["direct"][identity] & registry_roles
        }
        mode_counts = Counter(str(row["mode"]) for row in health)
        status_counts = Counter(str(row.get("status") or "unknown") for row in health)

        current_sources = [row for row in health if str(row.get("scope") or "current") == "current"]
        historical_sources = [row for row in health if str(row.get("scope")) == "historical"]
        complete_enumerators = sum(
            bool(row["complete"])
            and str(row["mode"]) == "board"
            and str(row.get("status")) == "ok"
            for row in current_sources
        )
        historical_enumerators = sum(
            bool(row["complete"])
            and str(row["mode"]) == "board"
            and str(row.get("status")) == "ok"
            for row in historical_sources
        )

        def has_note(row: dict[str, object], phrase: str) -> bool:
            return phrase in str(row.get("note") or "")

        actionable = [
            row
            for row in current_sources
            if row.get("last_error")
            or str(row.get("status")) in {"broken", "truncated", "empty"}
        ]
        access_limited = [
            row
            for row in current_sources
            if str(row.get("status")) == "blocked" or has_note(row, "access-blocked")
        ]
        stale_verifications = [
            row
            for row in current_sources
            if str(row["mode"]) == "verification"
            and (str(row.get("status")) == "stale" or has_note(row, "stale/closed"))
        ]
        unstructured_verifications = [
            row
            for row in current_sources
            if str(row["mode"]) == "verification"
            and (str(row.get("status")) == "unstructured" or has_note(row, "without JobPosting"))
        ]
        dormant_watches = [
            row
            for row in historical_sources
            if str(row.get("status")) in {"dormant", "empty", "stale"}
        ]
        historical_failures = [row for row in historical_sources if row.get("last_error")]
        truncated = [
            row
            for row in current_sources
            if row["expected_rows"] is not None
            and int(row["rows_scanned"] or 0) < int(row["expected_rows"])
            and str(row["mode"]) == "board"
        ]
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
                "productive_direct_sources": len(productive_direct_sources),
                "direct_date_coverage_percent": round(
                    100 * len(dated_direct_applications) / len(identities["direct"]), 1
                )
                if identities["direct"]
                else None,
                "direct_matches": len(direct_matches),
                "independent_matches": len(independent_matches),
                "registry_only": len(registry_only),
                "direct_only": len(direct_only),
                "registry_recall_percent": registry_recall,
                "benchmark_size": benchmark_size,
            },
            "contract": {
                "run_id": latest_run,
                "configured_sources": len(health),
                "current_sources": len(current_sources),
                "historical_sources": len(historical_sources),
                "complete_enumerators": complete_enumerators,
                "historical_enumerators": historical_enumerators,
                "actionable_anomalies": len(actionable),
                "access_limited": len(access_limited),
                "stale_verifications": len(stale_verifications),
                "unstructured_verifications": len(unstructured_verifications),
                "dormant_watches": len(dormant_watches),
                "historical_failures": len(historical_failures),
                "truncated_sources": len(truncated),
                "modes": dict(mode_counts),
                "statuses": dict(status_counts),
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
