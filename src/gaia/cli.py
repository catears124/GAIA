from __future__ import annotations

import argparse
import asyncio
import logging
import os
import threading
from pathlib import Path

import psycopg
import uvicorn
from psycopg import sql

from .db import Database, _normalize_database_url
from .live_inventory import InventoryWorker, LiveDatabase

LOGGER = logging.getLogger("gaia.cli")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="gaia")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or upgrade the PostgreSQL schema")

    worker = sub.add_parser("worker", help="run the continuous job-inventory worker")
    worker.add_argument("--once", action="store_true", help="drain currently due work, then exit")
    worker.add_argument("--budget-seconds", type=float, default=None)
    worker.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("GAIA_WORKER_CONCURRENCY", "24")),
    )

    serve = sub.add_parser("serve", help="serve the app and continuously refresh inventory")
    serve.add_argument("--host", default=os.getenv("GAIA_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("GAIA_PORT", "8501")))
    serve.add_argument(
        "--no-worker",
        action="store_true",
        help="serve read-only without starting the local continuous worker",
    )
    serve.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("GAIA_WORKER_CONCURRENCY", "24")),
    )
    return root


async def run_worker(*, once: bool, budget_seconds: float | None, concurrency: int) -> None:
    # Schema changes are explicit through `gaia migrate`; workers stay on the normal
    # pooled connection and never attempt long-running DDL during startup.
    database = LiveDatabase(migrate=False)
    summary = await InventoryWorker(database, concurrency=max(1, concurrency)).run(
        once=once,
        budget_seconds=budget_seconds,
    )
    if once:
        print(summary.as_dict())


def start_embedded_worker(concurrency: int) -> threading.Thread:
    def target() -> None:
        try:
            asyncio.run(run_worker(once=False, budget_seconds=None, concurrency=concurrency))
        except Exception:
            LOGGER.exception("embedded inventory worker stopped")

    thread = threading.Thread(target=target, name="gaia-inventory", daemon=True)
    thread.start()
    return thread


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


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=os.getenv("GAIA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "migrate":
        run_migration()
        print("PostgreSQL schema is ready.")
    elif args.command == "worker":
        asyncio.run(
            run_worker(
                once=args.once,
                budget_seconds=args.budget_seconds,
                concurrency=args.concurrency,
            )
        )
    elif args.command == "serve":
        if not args.no_worker:
            start_embedded_worker(max(1, args.concurrency))
        uvicorn.run("gaia.product_api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
