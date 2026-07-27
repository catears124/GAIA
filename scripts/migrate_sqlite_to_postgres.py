"""Import the former GAIA SQLite database into PostgreSQL/Supabase.

Run once after setting GAIA_DATABASE_URL to the Supabase transaction-pooler URL:

    python scripts/migrate_sqlite_to_postgres.py --source data/gaia.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gaia.db import Database  # noqa: E402


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


def insert_batches(database: Database, query: str, rows: Iterable[Sequence[Any]], size: int) -> int:
    total = 0
    with database.connect() as target:
        for batch in chunks(rows, size):
            target.executemany(query, batch)
            total += len(batch)
    return total


def import_database(source_path: Path, database: Database, *, batch_size: int, truncate: bool) -> None:
    if not source_path.exists():
        raise SystemExit(f"source database does not exist: {source_path}")

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
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
                (
                    (
                        row["id"],
                        row["started_at"],
                        row["finished_at"],
                        row["status"],
                        row["sources"],
                        row["postings"],
                        row["failed"],
                    )
                    for row in sync_rows
                ),
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
                (
                    (
                        row["posting_key"],
                        row["family_key"],
                        row["company"],
                        row["title"],
                        row["normalized_title"],
                        parsed_json(row["locations_json"], []),
                        row["apply_url"],
                        row["canonical_apply_url"],
                        row["source"],
                        row["source_id"],
                        row["source_mode"],
                        row["description"],
                        row["employment_type"],
                        row["posted_at"],
                        row["updated_at"],
                        row["posted_raw"],
                        row["posted_precision"],
                        row["posted_confidence"],
                        row["first_seen_at"],
                        row["last_seen_at"],
                        bool(row["active"]),
                        row["category"],
                        row["season"],
                        row["year"],
                        row["target_match"],
                        value(row, available, "link_checked_at"),
                        value(row, available, "link_http_status"),
                        value(row, available, "link_final_url"),
                        value(row, available, "link_status", "unchecked"),
                    )
                    for row in posting_rows
                ),
                batch_size,
            )

        catalog_count = 0
        if table_exists(source, "source_catalog"):
            catalog_rows = source.execute("SELECT * FROM source_catalog")
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
                (
                    (
                        row["source"],
                        row["kind"],
                        row["scope"],
                        Jsonb(parsed_json(row["spec_json"], {})),
                        row["first_discovered_at"],
                        row["last_discovered_at"],
                    )
                    for row in catalog_rows
                ),
                batch_size,
            )

        health_count = 0
        if table_exists(source, "source_health"):
            available = columns(source, "source_health")
            health_rows = source.execute("SELECT * FROM source_health")
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
                (
                    (
                        row["source"],
                        row["mode"],
                        bool(row["complete"]),
                        row["rows_scanned"],
                        row["expected_rows"],
                        row["target_rows"],
                        row["last_attempt_at"],
                        row["last_success_at"],
                        row["last_error"],
                        value(row, available, "status", "unknown"),
                        value(row, available, "scope", "current"),
                        value(row, available, "note"),
                        (
                            int(value(row, available, "last_run_id"))
                            if value(row, available, "last_run_id") in run_ids
                            else None
                        ),
                        value(row, available, "lifecycle", "candidate"),
                        int(value(row, available, "consecutive_failures", 0) or 0),
                    )
                    for row in health_rows
                ),
                batch_size,
            )

        benchmark_count = 0
        if table_exists(source, "benchmark_cases"):
            benchmark_rows = source.execute("SELECT * FROM benchmark_cases")
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
                (
                    (
                        row["version"],
                        row["posting_key"],
                        row["company"],
                        row["title"],
                        row["employment_type"],
                        row["source_mode"],
                        row["expected_category"],
                        row["expected_target_match"],
                        row["captured_at"],
                    )
                    for row in benchmark_rows
                ),
                batch_size,
            )

        database.rebuild_families()
        with database.connect() as target:
            family_count = target.execute("SELECT COUNT(*) AS count FROM families").fetchone()[
                "count"
            ]

        print(
            "Imported "
            f"{sync_count:,} sync runs, {posting_count:,} postings, "
            f"{catalog_count:,} catalog sources, {health_count:,} health rows, "
            f"{benchmark_count:,} benchmark cases; rebuilt {int(family_count):,} families."
        )
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
