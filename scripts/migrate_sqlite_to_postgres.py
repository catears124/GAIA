"""Import the former GAIA SQLite database into PostgreSQL/Supabase.

Run once after configuring a PostgreSQL connection:

    python scripts/migrate_sqlite_to_postgres.py --source data/gaia.db

The importer deliberately enforces the current PostgreSQL persistence contract.
Malformed legacy rows are skipped and reported instead of weakening database
constraints or aborting the entire migration.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gaia.db import Database  # noqa: E402
from gaia.grouping import normalize_title  # noqa: E402
from gaia.models import canonical_url  # noqa: E402
from gaia.quality import normalize_locations  # noqa: E402

VALID_SYNC_STATUSES = {"running", "ok", "partial", "cancelled"}
VALID_SCOPES = {"current", "historical"}
VALID_LIFECYCLES = {"candidate", "productive", "quarantined"}


def chunks(rows: Iterable[Sequence[Any]], size: int) -> Iterator[list[Sequence[Any]]]:
    batch: list[Sequence[Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def value(row: sqlite3.Row, available: set[str], name: str, default: Any = None) -> Any:
    return row[name] if name in available else default


def parsed_json(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def clean_text(raw: Any, default: str = "") -> str:
    if raw is None:
        return default
    return str(raw).strip()


def nonnegative_int(raw: Any, default: int = 0) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def optional_nonnegative_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def optional_year(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return None
    return year if -32768 <= year <= 32767 else None


def insert_batches(
    database: Database,
    query: str,
    rows: Iterable[Sequence[Any]],
    size: int,
) -> int:
    total = 0
    with database.connect() as target:
        for batch in chunks(rows, size):
            target.executemany(query, batch)
            total += len(batch)
    return total


def posting_records(
    rows: Iterable[sqlite3.Row],
    available: set[str],
    stats: dict[str, Any],
) -> Iterator[Sequence[Any]]:
    for row in rows:
        stats["seen"] += 1

        posting_key = clean_text(row["posting_key"])
        family_key = clean_text(row["family_key"])
        company = clean_text(row["company"])
        title = clean_text(row["title"])
        apply_url = clean_text(row["apply_url"])
        canonical_apply_url = clean_text(row["canonical_apply_url"])
        source = clean_text(row["source"])
        source_id = clean_text(row["source_id"])

        missing = [
            name
            for name, item in (
                ("posting_key", posting_key),
                ("family_key", family_key),
                ("company", company),
                ("title", title),
                ("apply_url", apply_url),
                ("canonical_apply_url", canonical_apply_url),
                ("source", source),
                ("source_id", source_id),
            )
            if not item
        ]
        if missing:
            stats["skipped"] += 1
            if len(stats["examples"]) < 10:
                stats["examples"].append(
                    f"{posting_key or '<unknown>'}: missing {', '.join(missing)}"
                )
            continue

        first_seen_at = (
            row["first_seen_at"]
            or row["last_seen_at"]
            or row["posted_at"]
            or row["updated_at"]
        )
        last_seen_at = row["last_seen_at"] or first_seen_at
        if first_seen_at is None or last_seen_at is None:
            stats["skipped"] += 1
            if len(stats["examples"]) < 10:
                stats["examples"].append(
                    f"{posting_key}: missing first_seen_at/last_seen_at"
                )
            continue

        raw_locations = parsed_json(row["locations_json"], [])
        if not isinstance(raw_locations, list):
            raw_locations = []
        locations = normalize_locations(
            [clean_text(item) for item in raw_locations if clean_text(item)]
        )

        normalized = clean_text(row["normalized_title"]) or normalize_title(title)
        canonical_apply_url = canonical_apply_url or canonical_url(apply_url)
        target_match = clean_text(row["target_match"], "unknown")
        description = clean_text(row["description"])
        if target_match not in {"exact", "year_confirmed", "source_confirmed"}:
            description = ""

        yield (
            posting_key,
            family_key,
            company,
            title,
            normalized,
            locations,
            apply_url,
            canonical_apply_url,
            source,
            source_id,
            clean_text(row["source_mode"], "direct"),
            description,
            clean_text(row["employment_type"]),
            row["posted_at"],
            row["updated_at"],
            row["posted_raw"],
            clean_text(row["posted_precision"], "unknown"),
            clean_text(row["posted_confidence"], "unknown"),
            first_seen_at,
            last_seen_at,
            bool(row["active"]),
            clean_text(row["category"], "other"),
            row["season"],
            optional_year(row["year"]),
            target_match,
            value(row, available, "link_checked_at"),
            optional_nonnegative_int(value(row, available, "link_http_status")),
            value(row, available, "link_final_url"),
            clean_text(value(row, available, "link_status"), "unchecked"),
        )


def import_database(
    source_path: Path,
    database: Database,
    *,
    batch_size: int,
    truncate: bool,
) -> None:
    if not source_path.exists():
        raise SystemExit(f"source database does not exist: {source_path}")

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    imported_at = datetime.now(UTC)
    try:
        if truncate:
            with database.connect() as target:
                target.execute(
                    """
                    TRUNCATE TABLE families, benchmark_cases, source_health,
                                   source_catalog, postings, sync_runs
                    RESTART IDENTITY CASCADE
                    """
                )

        sync_count = 0
        run_ids: set[int] = set()
        if table_exists(source, "sync_runs"):
            sync_rows = source.execute(
                """
                SELECT id, started_at, finished_at, status, sources, postings, failed
                FROM sync_runs ORDER BY id
                """
            ).fetchall()
            run_ids = {int(row["id"]) for row in sync_rows}
            running_ids = [
                int(row["id"])
                for row in sync_rows
                if clean_text(row["status"]) == "running"
            ]
            current_running_id = max(running_ids) if running_ids else None

            def normalized_sync_rows() -> Iterator[Sequence[Any]]:
                for row in sync_rows:
                    status = clean_text(row["status"], "partial")
                    if status not in VALID_SYNC_STATUSES:
                        status = "partial"
                    finished_at = row["finished_at"]
                    if status == "running" and int(row["id"]) != current_running_id:
                        status = "cancelled"
                        finished_at = finished_at or row["started_at"]
                    yield (
                        row["id"],
                        row["started_at"],
                        finished_at,
                        status,
                        nonnegative_int(row["sources"]),
                        nonnegative_int(row["postings"]),
                        nonnegative_int(row["failed"]),
                    )

            sync_count = insert_batches(
                database,
                """
                INSERT INTO sync_runs(id, started_at, finished_at, status, sources, postings, failed)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at,
                    status=excluded.status,
                    sources=excluded.sources,
                    postings=excluded.postings,
                    failed=excluded.failed
                """,
                normalized_sync_rows(),
                batch_size,
            )
            with database.connect() as target:
                target.execute(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('sync_runs', 'id'),
                        COALESCE((SELECT MAX(id) FROM sync_runs), 1),
                        EXISTS (SELECT 1 FROM sync_runs)
                    )
                    """
                )

        posting_count = 0
        posting_stats: dict[str, Any] = {"seen": 0, "skipped": 0, "examples": []}
        if table_exists(source, "postings"):
            available = columns(source, "postings")
            posting_rows = source.execute("SELECT * FROM postings")
            posting_count = insert_batches(
                database,
                """
                INSERT INTO postings(
                    posting_key, family_key, company, title, normalized_title, locations,
                    apply_url, canonical_apply_url, source, source_id, source_mode, description,
                    employment_type, posted_at, updated_at, posted_raw, posted_precision,
                    posted_confidence, first_seen_at, last_seen_at, active, category, season,
                    year, target_match, link_checked_at, link_http_status, link_final_url, link_status
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT(posting_key) DO UPDATE SET
                    family_key=excluded.family_key,
                    company=excluded.company,
                    title=excluded.title,
                    normalized_title=excluded.normalized_title,
                    locations=excluded.locations,
                    apply_url=excluded.apply_url,
                    canonical_apply_url=excluded.canonical_apply_url,
                    source=excluded.source,
                    source_id=excluded.source_id,
                    source_mode=excluded.source_mode,
                    description=excluded.description,
                    employment_type=excluded.employment_type,
                    posted_at=excluded.posted_at,
                    updated_at=excluded.updated_at,
                    posted_raw=excluded.posted_raw,
                    posted_precision=excluded.posted_precision,
                    posted_confidence=excluded.posted_confidence,
                    first_seen_at=excluded.first_seen_at,
                    last_seen_at=excluded.last_seen_at,
                    active=excluded.active,
                    category=excluded.category,
                    season=excluded.season,
                    year=excluded.year,
                    target_match=excluded.target_match,
                    link_checked_at=excluded.link_checked_at,
                    link_http_status=excluded.link_http_status,
                    link_final_url=excluded.link_final_url,
                    link_status=excluded.link_status
                """,
                posting_records(posting_rows, available, posting_stats),
                batch_size,
            )

        catalog_count = 0
        if table_exists(source, "source_catalog"):
            catalog_rows = source.execute("SELECT * FROM source_catalog")

            def catalog_records() -> Iterator[Sequence[Any]]:
                for row in catalog_rows:
                    source_name = clean_text(row["source"])
                    kind = clean_text(row["kind"], "unknown")
                    if not source_name:
                        continue
                    scope = clean_text(row["scope"], "historical")
                    if scope not in VALID_SCOPES:
                        scope = "historical"
                    first = row["first_discovered_at"] or row["last_discovered_at"] or imported_at
                    last = row["last_discovered_at"] or first
                    yield (
                        source_name,
                        kind,
                        scope,
                        Jsonb(parsed_json(row["spec_json"], {})),
                        first,
                        last,
                    )

            catalog_count = insert_batches(
                database,
                """
                INSERT INTO source_catalog(
                    source, kind, scope, spec, first_discovered_at, last_discovered_at
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(source) DO UPDATE SET
                    kind=excluded.kind,
                    scope=excluded.scope,
                    spec=excluded.spec,
                    first_discovered_at=excluded.first_discovered_at,
                    last_discovered_at=excluded.last_discovered_at
                """,
                catalog_records(),
                batch_size,
            )

        health_count = 0
        if table_exists(source, "source_health"):
            available = columns(source, "source_health")
            health_rows = source.execute("SELECT * FROM source_health")

            def health_records() -> Iterator[Sequence[Any]]:
                for row in health_rows:
                    source_name = clean_text(row["source"])
                    mode = clean_text(row["mode"], "unknown")
                    if not source_name:
                        continue
                    scope = clean_text(value(row, available, "scope"), "current")
                    if scope not in VALID_SCOPES:
                        scope = "current"
                    lifecycle = clean_text(
                        value(row, available, "lifecycle"), "candidate"
                    )
                    if lifecycle not in VALID_LIFECYCLES:
                        lifecycle = "candidate"
                    last_attempt = (
                        row["last_attempt_at"] or row["last_success_at"] or imported_at
                    )
                    raw_run_id = value(row, available, "last_run_id")
                    last_run_id = (
                        int(raw_run_id)
                        if raw_run_id is not None and int(raw_run_id) in run_ids
                        else None
                    )
                    yield (
                        source_name,
                        mode,
                        bool(row["complete"]),
                        nonnegative_int(row["rows_scanned"]),
                        optional_nonnegative_int(row["expected_rows"]),
                        nonnegative_int(row["target_rows"]),
                        last_attempt,
                        row["last_success_at"],
                        row["last_error"],
                        clean_text(value(row, available, "status"), "unknown"),
                        scope,
                        value(row, available, "note"),
                        last_run_id,
                        lifecycle,
                        nonnegative_int(
                            value(row, available, "consecutive_failures", 0)
                        ),
                    )

            health_count = insert_batches(
                database,
                """
                INSERT INTO source_health(
                    source, mode, complete, rows_scanned, expected_rows, target_rows,
                    last_attempt_at, last_success_at, last_error, status, scope, note,
                    last_run_id, lifecycle, consecutive_failures
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(source) DO UPDATE SET
                    mode=excluded.mode,
                    complete=excluded.complete,
                    rows_scanned=excluded.rows_scanned,
                    expected_rows=excluded.expected_rows,
                    target_rows=excluded.target_rows,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at,
                    last_error=excluded.last_error,
                    status=excluded.status,
                    scope=excluded.scope,
                    note=excluded.note,
                    last_run_id=excluded.last_run_id,
                    lifecycle=excluded.lifecycle,
                    consecutive_failures=excluded.consecutive_failures
                """,
                health_records(),
                batch_size,
            )

        benchmark_count = 0
        if table_exists(source, "benchmark_cases"):
            benchmark_rows = source.execute("SELECT * FROM benchmark_cases")

            def benchmark_records() -> Iterator[Sequence[Any]]:
                for row in benchmark_rows:
                    version = clean_text(row["version"])
                    posting_key = clean_text(row["posting_key"])
                    if not version or not posting_key:
                        continue
                    yield (
                        version,
                        posting_key,
                        clean_text(row["company"]),
                        clean_text(row["title"]),
                        clean_text(row["employment_type"]),
                        clean_text(row["source_mode"]),
                        clean_text(row["expected_category"]),
                        clean_text(row["expected_target_match"]),
                        row["captured_at"] or imported_at,
                    )

            benchmark_count = insert_batches(
                database,
                """
                INSERT INTO benchmark_cases(
                    version, posting_key, company, title, employment_type, source_mode,
                    expected_category, expected_target_match, captured_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(version, posting_key) DO UPDATE SET
                    company=excluded.company,
                    title=excluded.title,
                    employment_type=excluded.employment_type,
                    source_mode=excluded.source_mode,
                    expected_category=excluded.expected_category,
                    expected_target_match=excluded.expected_target_match,
                    captured_at=excluded.captured_at
                """,
                benchmark_records(),
                batch_size,
            )

        database.rebuild_families()
        with database.connect() as target:
            family_count = target.execute(
                "SELECT COUNT(*) AS count FROM families"
            ).fetchone()["count"]

        print(
            "Imported "
            f"{sync_count:,} sync runs, {posting_count:,} postings, "
            f"{catalog_count:,} catalog sources, {health_count:,} health rows, "
            f"{benchmark_count:,} benchmark cases; rebuilt {int(family_count):,} families."
        )
        if posting_stats["skipped"]:
            print(
                f"Skipped {posting_stats['skipped']:,} of {posting_stats['seen']:,} "
                "legacy postings that violated the current persistence contract."
            )
            for example in posting_stats["examples"]:
                print(f"  - {example}")
    finally:
        source.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/gaia.db"))
    parser.add_argument("--schema", default=None)
    parser.add_argument("--batch-size", type=int, default=2_000)
    parser.add_argument(
        "--keep-target",
        action="store_true",
        help="upsert into existing PostgreSQL rows instead of truncating first",
    )
    args = parser.parse_args()
    database = Database(schema=args.schema, migrate=True)
    import_database(
        args.source,
        database,
        batch_size=max(1, args.batch_size),
        truncate=not args.keep_target,
    )


if __name__ == "__main__":
    main()
