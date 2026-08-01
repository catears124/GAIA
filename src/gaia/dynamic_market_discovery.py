from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .collectors import Collector
from .config import load_sources
from .db import Database
from .discovery import collectors_from_registry
from .inventory_runtime import (
    COVERAGE_KINDS,
    VALID_CANDIDATE_STATUSES,
    InventoryWorker,
)
from .market_discovery import discover_github_market
from .models import Posting
from .provider_discovery import provider_collectors_from_postings
from .quality import canonical_source_name
from .source_catalog import _collector, _spec, merge_catalog, save_candidates

LOGGER = logging.getLogger("gaia.dynamic-market-discovery")
SNAPSHOT_VERSION = 1


def candidate_collectors(
    postings: list[Posting],
    settings: dict[str, Any],
) -> list[Collector]:
    """Turn untrusted market leads into probe-only employer source candidates."""
    generated = merge_catalog(
        collectors_from_registry(postings, settings, deep=True),
        provider_collectors_from_postings(postings),
    )
    candidates: list[Collector] = []
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


def serialize_candidates(candidates: list[Collector]) -> list[dict[str, Any]]:
    """Serialize probe-only candidates without treating external indexes as trusted data."""
    rows: list[dict[str, Any]] = []
    for collector in candidates:
        described = _spec(collector)
        if described is None:
            continue
        kind, spec = described
        if kind not in COVERAGE_KINDS:
            continue
        source = canonical_source_name(collector.name)
        if not source:
            continue
        rows.append(
            {
                "source": source,
                "kind": kind,
                "scope": collector.scope,
                "spec": spec,
            }
        )
    return rows


def deserialize_candidates(rows: list[dict[str, Any]]) -> list[Collector]:
    """Restore only supported source candidates from a discovery snapshot."""
    candidates: list[Collector] = []
    seen: set[str] = set()
    for row in rows:
        kind = str(row.get("kind") or "")
        spec = row.get("spec")
        source = canonical_source_name(str(row.get("source") or ""))
        if kind not in COVERAGE_KINDS or not isinstance(spec, dict) or not source:
            continue
        try:
            collector = _collector(kind, spec)
        except (KeyError, TypeError, ValueError):
            continue
        if collector is None or source in seen:
            continue
        collector.name = source
        collector.scope = str(row.get("scope") or "current")
        candidates.append(collector)
        seen.add(source)
    return candidates


def posting_freshness(postings: list[Posting]) -> dict[str, Any]:
    """Measure employer-provided dates instead of equating rediscovery with freshness."""
    now = datetime.now(UTC)
    dated: list[datetime] = []
    for posting in postings:
        if posting.posted_at is None:
            continue
        value = posting.posted_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        dated.append(value.astimezone(UTC))

    freshest = max(dated) if dated else None
    return {
        "dated_postings": len(dated),
        "freshest_employer_posted_at": freshest.isoformat() if freshest else None,
        "employer_posted_last_24h": sum(value >= now - timedelta(days=1) for value in dated),
        "employer_posted_last_72h": sum(value >= now - timedelta(days=3) for value in dated),
        "employer_posted_last_7d": sum(value >= now - timedelta(days=7) for value in dated),
    }


def balanced_probe_candidates(candidates: list[Collector], limit: int) -> list[Collector]:
    """Probe across provider types rather than exhausting one large ATS bucket first."""
    buckets: dict[str, deque[Collector]] = defaultdict(deque)
    for collector in candidates:
        described = _spec(collector)
        if described is None:
            continue
        kind, _specification = described
        buckets[kind].append(collector)

    selected: list[Collector] = []
    kinds = deque(sorted(buckets))
    while kinds and len(selected) < max(1, limit):
        kind = kinds.popleft()
        bucket = buckets[kind]
        if bucket:
            selected.append(bucket.popleft())
        if bucket:
            kinds.append(kind)
    return selected


def _client(concurrency: int) -> httpx.AsyncClient:
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
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
    )


async def build_market_snapshot(
    *,
    settings: dict[str, Any] | None = None,
    concurrency: int,
) -> dict[str, Any]:
    """Search public indexes without requiring production database availability."""
    settings = settings or load_sources()
    async with _client(concurrency) as client:
        postings, health = await discover_github_market(client, settings)

    search_results = [result for result in health if result.mode == "market-discovery"]
    successful_searches = sum(
        result.complete and result.error is None and result.status == "loaded"
        for result in search_results
    )
    if search_results and successful_searches == 0:
        raise RuntimeError("every dynamic GitHub market query failed or was blocked")

    candidates = candidate_collectors(postings, settings)
    serialized = serialize_candidates(candidates)
    kind_counts = Counter(str(row["kind"]) for row in serialized)
    summary: dict[str, Any] = {
        "enabled": bool(search_results),
        "queries": len(search_results),
        "successful_queries": successful_searches,
        "market_postings": len(postings),
        "explicit_target_postings": sum(
            posting.target_match == "exact" for posting in postings
        ),
        "candidate_sources": len(serialized),
        "candidate_source_kinds": dict(sorted(kind_counts.items())),
        **posting_freshness(postings),
    }
    return {
        "version": SNAPSHOT_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "summary": summary,
        "candidates": serialized,
    }


async def audit_market_snapshot(
    snapshot: dict[str, Any],
    *,
    probe_limit: int,
    concurrency: int,
) -> dict[str, Any]:
    """Probe official boards directly when the production database cannot accept writes."""
    if int(snapshot.get("version") or 0) != SNAPSHOT_VERSION:
        raise ValueError("unsupported dynamic market snapshot version")
    rows = snapshot.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("dynamic market snapshot is missing candidates")

    candidates = deserialize_candidates([row for row in rows if isinstance(row, dict)])
    selected = balanced_probe_candidates(candidates, probe_limit)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async with _client(concurrency) as client:
        async def probe(collector: Collector):
            async with semaphore:
                try:
                    return collector, await collector.collect(client), None
                except Exception as exc:  # noqa: BLE001 - one bad employer must not abort the audit
                    return collector, None, repr(exc)

        outcomes = await asyncio.gather(*(probe(collector) for collector in selected))

    official_postings: list[Posting] = []
    valid_sources = 0
    sources_with_postings = 0
    source_evidence: list[dict[str, Any]] = []
    for collector, result, exception in outcomes:
        valid = bool(
            result is not None
            and result.complete
            and result.error is None
            and result.status in VALID_CANDIDATE_STATUSES
        )
        postings = list(result.postings) if valid and result is not None else []
        if valid:
            valid_sources += 1
            official_postings.extend(postings)
            sources_with_postings += bool(postings)
        source_dates = posting_freshness(postings)
        source_evidence.append(
            {
                "source": collector.name,
                "valid": valid,
                "status": result.status if result is not None else "exception",
                "complete": bool(result.complete) if result is not None else False,
                "rows": len(postings),
                "freshest_employer_posted_at": source_dates[
                    "freshest_employer_posted_at"
                ],
                "error": exception or (result.error if result is not None else None),
            }
        )

    freshness = posting_freshness(official_postings)
    audit_summary = {
        "stateless_sources_probed": len(selected),
        "stateless_sources_valid": valid_sources,
        "stateless_sources_with_postings": sources_with_postings,
        "stateless_official_postings": len(official_postings),
        "stateless_dated_postings": freshness["dated_postings"],
        "stateless_freshest_employer_posted_at": freshness[
            "freshest_employer_posted_at"
        ],
        "stateless_employer_posted_last_24h": freshness["employer_posted_last_24h"],
        "stateless_employer_posted_last_72h": freshness["employer_posted_last_72h"],
        "stateless_employer_posted_last_7d": freshness["employer_posted_last_7d"],
    }
    snapshot["audit"] = {
        "captured_at": datetime.now(UTC).isoformat(),
        "summary": audit_summary,
        "sources": source_evidence,
    }
    base_summary = snapshot.get("summary")
    if isinstance(base_summary, dict):
        base_summary.update(audit_summary)
    return audit_summary


async def ingest_market_snapshot(
    database: Database,
    snapshot: dict[str, Any],
    *,
    probe_limit: int,
    concurrency: int,
) -> dict[str, Any]:
    """Validate captured source evidence before adding it to production inventory."""
    if int(snapshot.get("version") or 0) != SNAPSHOT_VERSION:
        raise ValueError("unsupported dynamic market snapshot version")
    rows = snapshot.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("dynamic market snapshot is missing candidates")

    worker = InventoryWorker(database, concurrency=concurrency)
    candidates = deserialize_candidates([row for row in rows if isinstance(row, dict)])
    saved = save_candidates(database, candidates, origin="dynamic-github-market")

    async with _client(concurrency) as client:
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
    base_summary = snapshot.get("summary")
    summary: dict[str, Any] = dict(base_summary) if isinstance(base_summary, dict) else {}
    summary.update(
        {
            "candidate_rows_written": saved,
            "candidate_sources_probed": len(claimed),
            "candidate_sources_promoted": promoted,
            "catalog_sources_scheduled": catalog_sources,
        }
    )
    LOGGER.info("dynamic market discovery %s", summary)
    return summary


async def run_dynamic_market_discovery(
    database: Database,
    *,
    probe_limit: int,
    concurrency: int,
) -> dict[str, Any]:
    """Search, capture, validate, and schedule newly discovered employer sources."""
    snapshot = await build_market_snapshot(concurrency=concurrency)
    return await ingest_market_snapshot(
        database,
        snapshot,
        probe_limit=probe_limit,
        concurrency=concurrency,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaia-dynamic-market-discovery")
    parser.add_argument(
        "--probe-limit",
        type=int,
        default=int(os.getenv("GAIA_CANDIDATE_PROBE_LIMIT", "96")),
    )
    parser.add_argument(
        "--audit-probe-limit",
        type=int,
        default=int(os.getenv("GAIA_STATELESS_AUDIT_LIMIT", "96")),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("GAIA_WORKER_CONCURRENCY", "16")),
    )
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--snapshot-output", type=Path, default=None)
    parser.add_argument("--snapshot-input", type=Path, default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=os.getenv("GAIA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    concurrency = max(1, args.concurrency)

    if args.snapshot_input is not None:
        snapshot = json.loads(args.snapshot_input.read_text(encoding="utf-8"))
    else:
        snapshot = asyncio.run(build_market_snapshot(concurrency=concurrency))

    audit_summary: dict[str, Any] | None = None
    if args.audit_only:
        audit_summary = asyncio.run(
            audit_market_snapshot(
                snapshot,
                probe_limit=max(1, args.audit_probe_limit),
                concurrency=concurrency,
            )
        )

    if args.snapshot_output is not None:
        args.snapshot_output.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if args.audit_only:
        print(json.dumps(audit_summary or {}, sort_keys=True))
        return
    if args.search_only:
        print(json.dumps(snapshot.get("summary") or {}, sort_keys=True))
        return

    database = Database(migrate=False)
    summary = asyncio.run(
        ingest_market_snapshot(
            database,
            snapshot,
            probe_limit=max(1, args.probe_limit),
            concurrency=concurrency,
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
