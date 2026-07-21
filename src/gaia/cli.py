from __future__ import annotations

import argparse
import asyncio
import logging
import os

import uvicorn

from .db import Database
from .service import SyncService


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="gaia")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="refresh current internship sources")
    sub.add_parser(
        "discover",
        help="expand the company/source universe, then collect all discovered sources",
    )
    serve = sub.add_parser("serve", help="serve the local web application")
    serve.add_argument("--host", default=os.getenv("GAIA_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("GAIA_PORT", "8501")))
    return root


async def run_sync(mode: str) -> None:
    concurrency = max(1, int(os.getenv("GAIA_CONCURRENCY", "16")))
    summary = await SyncService(Database(), concurrency=concurrency).sync(mode=mode)
    print(summary.as_dict())


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=os.getenv("GAIA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "sync":
        asyncio.run(run_sync("refresh"))
    elif args.command == "discover":
        asyncio.run(run_sync("discover"))
    elif args.command == "serve":
        uvicorn.run("gaia.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
