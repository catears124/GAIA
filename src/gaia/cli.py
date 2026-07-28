from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

import psycopg
import uvicorn
from psycopg import sql

from .db import Database, _normalize_database_url
from .employer_census import (
    ECOSYSTEM_SCHEMA_STATEMENTS,
    merge_observations_into_universe,
)
from .health import production_report
from .live_inventory import InventoryWorker, LiveDatabase
from .universe import (
    UNIVERSE_SCHEMA_STATEMENTS,
    rebuild_employer_universe,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="gaia")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or upgrade the PostgreSQL schema")
    sub.add_parser(
        "reconcile",
        help="serialize derived role families and the employer-universe census",
    )

    worker = sub.add_parser("worker", help="run a bounded production inventory batch")
    worker.add_argument("--once", action="store_true", help="drain currently due work, then exit")
    worker.add_argument("--budget-seconds", type=float, default=None)
    worker.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("GAIA_WORKER_CONCURRENCY", "24")),
    )

    check = sub.add_parser("check", help="verify hosted inventory health and progress")
    check.add_argument(
        "--max-activity-minutes",
        type=int,
        default=int(os.getenv("GAIA_CHECK_MAX_ACTIVITY_MINUTES", "90")),
    )
    check.add_argument(
        "--min-sources",
        type=int,
        default=int(os.getenv("GAIA_CHECK_MIN_SOURCES", "100")),
    )
    check.add_argument(
        "--min-active-listings",
        type=int,
        default=int(os.getenv("GAIA_CHECK_MIN_ACTIVE_LISTINGS", "25")),
    )
    check.add_argument(
        "--require-healthy",
        action="store_true",
        default=os.getenv("GAIA_CHECK_REQUIRE_HEALTHY", "0") == "1",
    )
    check.add_argument("--output", type=Path, default=None)

    serve = sub.add_parser("serve", help="preview the read-only product API locally")
    serve.add_argument("--host", default=os.getenv("GAIA_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("GAIA_PORT", "8501")))
    return root


async def run_worker(*, once: bool, budget_seconds: float | None, concurrency: int) -> None:
    # Production workers run in GitHub Actions against Supabase's pooled connection.
    # Schema changes are applied separately by the deployment workflow.
    database = LiveDatabase(migrate=False)
    summary = await InventoryWorker(database, concurrency=max(1, concurrency)).run(
        once=once,
        budget_seconds=budget_seconds,
    )
    if once:
        print(summary.as_dict())


def run_migration() -> None:
    """Run DDL on a direct/admin connection with no statement or lock timeout."""
    migration_url = (
        os.getenv("GAIA_MIGRATION_DATABASE_URL")
        or os.getenv("GAIA_ADMIN_DATABASE_URL")
        or os.getenv("POSTGRES_URL_NON_POOLING")
    )
    if migration_url:
        migration_url = _normalize_database_url(migration_url)

    database = Database(url=migration_url, migrate=False)
    schema_sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(
        database.url,
        connect_timeout=database.timeout,
        application_name="gaia-migrate",
        prepare_threshold=None,
        options="-c statement_timeout=0 -c lock_timeout=0",
    ) as connection:
        connection.execute("SET statement_timeout TO 0")
        connection.execute("SET lock_timeout TO 0")
        connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(database.schema))
        )
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(database.schema))
        )
        connection.execute(schema_sql)
        for statement in (*UNIVERSE_SCHEMA_STATEMENTS, *ECOSYSTEM_SCHEMA_STATEMENTS):
            connection.execute(statement)


def run_reconcile() -> dict[str, int]:
    """Build all global read models once after horizontally scaled collectors finish."""
    database = Database(migrate=False)
    with database.connect() as lock:
        lock.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            ("gaia:global-reconcile",),
        )
        try:
            database.rebuild_families()
            posting_census = rebuild_employer_universe(database)
            ecosystem_census = merge_observations_into_universe(database)
            return {
                **posting_census,
                **{f"ecosystem_{key}": value for key, value in ecosystem_census.items()},
            }
        finally:
            lock.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                ("gaia:global-reconcile",),
            )


def run_check(args: argparse.Namespace) -> int:
    database = Database(migrate=False)
    report = production_report(
        database,
        max_activity_minutes=max(1, args.max_activity_minutes),
        min_sources=max(1, args.min_sources),
        min_active_listings=max(1, args.min_active_listings),
        require_healthy=bool(args.require_healthy),
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


def main() -> int:
    args = parser().parse_args()
    logging.basicConfig(
        level=os.getenv("GAIA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "migrate":
        run_migration()
        print("PostgreSQL schema is ready.")
    elif args.command == "reconcile":
        print(json.dumps(run_reconcile(), sort_keys=True))
    elif args.command == "worker":
        asyncio.run(
            run_worker(
                once=args.once,
                budget_seconds=args.budget_seconds,
                concurrency=args.concurrency,
            )
        )
    elif args.command == "check":
        return run_check(args)
    elif args.command == "serve":
        uvicorn.run("gaia.product_api:app", host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
