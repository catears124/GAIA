from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

import httpx

from .db import Database
from .discovery import collectors_from_registry
from .inventory_runtime import COVERAGE_KINDS, InventoryWorker
from .market_discovery import discover_github_market
from .models import Posting
from .provider_discovery import provider_collectors_from_postings
from .quality import canonical_source_name
from .source_catalog import _spec, merge_catalog, save_candidates

LOGGER = logging.getLogger("gaia.dynamic-market-discovery")


def candidate_collectors(
    postings: list[Posting],
    settings: dict[str, Any],
):
    """Turn untrusted market leads into probe-only employer source candidates."""
    generated = merge_catalog(
        collectors_from_registry(postings, settings, deep=True),
        provider_collectors_from_postings(postings),
    )
    candidates = []
    for collector in generated:
        described = _spec(collector)
        if described is None:
            continue
        kind, _specification = described
        if kind not in COVERAGE_KINDS:
            continue
        source = canonical_source_name(collector.name)
        if not source:
            continue
        collector.name = source
        candidates.append(collector)
    return candidates


async def run_dynamic_market_discovery(
    database: Database,
    *,
    probe_limit: int,
    concurrency: int,
) -> dict[str, int | bool]:
    """Search public market indexes, validate recovered boards, and expand coverage safely."""
    worker = InventoryWorker(database, concurrency=concurrency)
    timeout = httpx.Timeout(float(os.getenv("GAIA_HTTP_TIMEOUT", "45")))
    limits = httpx.Limits(
        max_connections=max(32, concurrency * 3),
        max_keepalive_connections=max(16, concurrency * 2),
    )
    headers = {
        "User-Agent": os.getenv(
            "GAIA_USER_AGENT",
            "GAIA/5.0 continuous-job-inventory (+github.com/catears124/GAIA)",
        ),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
    ) as client:
        postings, health = await discover_github_market(client, worker.settings)
        await worker._apply_auxiliary_results(health)

        search_results = [result for result in health if result.mode == "market-discovery"]
        successful_searches = sum(
            result.complete and result.error is None and result.status == "loaded"
            for result in search_results
        )
        if search_results and successful_searches == 0:
            raise RuntimeError("every dynamic GitHub market query failed or was blocked")

        candidates = candidate_collectors(postings, worker.settings)
        saved = save_candidates(
            database,
            candidates,
            origin="dynamic-github-market",
        )

        claimed = worker.store.claim_candidates(
            limit=max(1, probe_limit),
            lease_seconds=worker.lease_seconds,
        )
        promoted = 0
        if claimed:
            promoted = sum(
                await asyncio.gather(
                    *(worker._probe_candidate(client, target) for target in claimed)
                )
            )

    catalog_sources = worker.store.sync_catalog()
    summary: dict[str, int | bool] = {
        "enabled": bool(search_results),
        "queries": len(search_results),
        "successful_queries": successful_searches,
        "market_postings": len(postings),
        "candidate_sources": len(candidates),
        "candidate_rows_written": saved,
        "candidate_sources_probed": len(claimed),
        "candidate_sources_promoted": promoted,
        "catalog_sources_scheduled": catalog_sources,
    }
    LOGGER.info("dynamic market discovery %s", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaia-dynamic-market-discovery")
    parser.add_argument(
        "--probe-limit",
        type=int,
        default=int(os.getenv("GAIA_CANDIDATE_PROBE_LIMIT", "96")),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("GAIA_WORKER_CONCURRENCY", "16")),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=os.getenv("GAIA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database = Database(migrate=False)
    summary = asyncio.run(
        run_dynamic_market_discovery(
            database,
            probe_limit=max(1, args.probe_limit),
            concurrency=max(1, args.concurrency),
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
