from __future__ import annotations

import argparse
import asyncio
import logging
import os

import uvicorn

from .db import Database
from .inventory import InventoryWorker


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
        default=int(os.getenv("GAIA_WORKER_CONCURRENCY", "12")),
    )

    serve = sub.add_parser("serve", help="serve the local web application")
    serve.add_argument("--host", default=os.getenv("GAIA_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("GAIA_PORT", "8501")))
    return root


async def run_worker(*, once: bool, budget_seconds: float | None, concurrency: int) -> None:
    database = Database(migrate=True)
    summary = await InventoryWorker(database, concurrency=max(1, concurrency)).run(
        once=once,
        budget_seconds=budget_seconds,
    )
    print(summary.as_dict())


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=os.getenv("GAIA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "migrate":
        Database(migrate=True)
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
        uvicorn.run("gaia.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
