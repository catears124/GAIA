from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .db import Database
from .inventory import ClaimedTarget
from .live_inventory import InventoryWorker, LiveInventoryStore

LOGGER = logging.getLogger("gaia.source_audit")
BAD_STATUSES = ("broken", "blocked", "truncated", "partial")
FRESHNESS_FLOOR_SECONDS = 90 * 60
FRESHNESS_INTERVAL_MULTIPLIER = 3


@dataclass(slots=True)
class AuditRow:
    source: str
    kind: str
    previous_status: str | None
    previous_complete_at: str | None
    outcome: str
    final_status: str | None = None
    final_complete_at: str | None = None
    rows_scanned: int | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def unhealthy_sources(database: Database, *, shard_index: int, shard_count: int) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT
                target.source,
                catalog.kind,
                catalog.scope,
                catalog.spec,
                target.interval_seconds,
                target.consecutive_failures,
                target.last_status,
                target.last_complete_at
            FROM crawl_targets AS target
            JOIN source_catalog AS catalog USING(source)
            WHERE target.enabled
              AND target.scheduled
              AND catalog.validated
              AND catalog.scope='current'
              AND (
                    target.last_complete_at IS NULL
                 OR target.last_complete_at < now() - make_interval(
                        secs => GREATEST(target.interval_seconds * %s, %s)
                    )
                 OR target.last_status = ANY(%s)
              )
            ORDER BY catalog.kind, target.last_complete_at NULLS FIRST, target.source
            """,
            (FRESHNESS_INTERVAL_MULTIPLIER, FRESHNESS_FLOOR_SECONDS, list(BAD_STATUSES)),
        ).fetchall()

    selected: list[dict[str, Any]] = []
    for row in rows:
        source = str(row["source"])
        bucket = int.from_bytes(hashlib.sha256(source.encode("utf-8")).digest()[:8], "big") % shard_count
        if bucket == shard_index:
            selected.append(dict(row))
    return selected


def claim_exact_source(
    store: LiveInventoryStore,
    source: str,
    *,
    lease_seconds: int,
) -> ClaimedTarget | None:
    with store.database.connect() as connection:
        row = connection.execute(
            """
            UPDATE crawl_targets AS target
            SET lease_owner=%s,
                lease_expires_at=now() + (%s * interval '1 second'),
                last_started_at=now(),
                next_run_at=LEAST(next_run_at, now()),
                updated_at=now()
            FROM source_catalog AS catalog
            WHERE target.source=%s
              AND catalog.source=target.source
              AND target.enabled
              AND target.scheduled
              AND catalog.validated
              AND catalog.scope='current'
              AND (target.lease_expires_at IS NULL OR target.lease_expires_at < now())
            RETURNING target.source, catalog.kind, catalog.scope, catalog.spec,
                      target.interval_seconds, target.consecutive_failures
            """,
            (store.worker_id, lease_seconds, source),
        ).fetchone()
    if row is None:
        return None
    return ClaimedTarget(
        source=str(row["source"]),
        kind=str(row["kind"]),
        scope=str(row["scope"]),
        spec=dict(row["spec"] or {}),
        interval_seconds=int(row["interval_seconds"]),
        consecutive_failures=int(row["consecutive_failures"] or 0),
    )


def final_state(database: Database, source: str) -> dict[str, Any]:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT last_status, last_complete_at, last_rows, last_error
            FROM crawl_targets
            WHERE source=%s
            """,
            (source,),
        ).fetchone()
    return dict(row) if row is not None else {}


async def audit_one(
    worker: InventoryWorker,
    client: httpx.AsyncClient,
    row: dict[str, Any],
    *,
    per_source_timeout: float,
) -> AuditRow:
    source = str(row["source"])
    started = time.monotonic()
    result = AuditRow(
        source=source,
        kind=str(row["kind"]),
        previous_status=str(row["last_status"]) if row.get("last_status") is not None else None,
        previous_complete_at=_iso(row.get("last_complete_at")),
        outcome="pending",
    )
    target = claim_exact_source(worker.store, source, lease_seconds=worker.lease_seconds)
    if target is None:
        result.outcome = "leased_elsewhere"
        result.elapsed_seconds = round(time.monotonic() - started, 3)
        return result

    try:
        await asyncio.wait_for(worker._run_target(client, target), timeout=per_source_timeout)
    except TimeoutError:
        result.outcome = "timeout"
        result.error = f"individual audit exceeded {per_source_timeout:.0f}s"
        try:
            worker.store.release_target(target, result.error)
        except Exception as exc:  # pragma: no cover - secondary failure telemetry
            result.error += f"; release failed: {exc!r}"
    except asyncio.CancelledError:
        try:
            worker.store.release_target(target, "source audit cancelled")
        finally:
            raise
    except Exception as exc:  # pragma: no cover - production containment
        LOGGER.exception("unhandled audit failure for %s", source)
        result.outcome = "exception"
        result.error = repr(exc)
        try:
            worker.store.release_target(target, result.error)
        except Exception:
            LOGGER.exception("could not release %s after audit failure", source)
    else:
        result.outcome = "probed"

    state = final_state(worker.database, source)
    result.final_status = str(state.get("last_status")) if state.get("last_status") is not None else None
    result.final_complete_at = _iso(state.get("last_complete_at"))
    result.rows_scanned = int(state.get("last_rows") or 0)
    result.error = result.error or (str(state.get("last_error")) if state.get("last_error") else None)
    result.elapsed_seconds = round(time.monotonic() - started, 3)
    return result


async def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    database = Database(migrate=False)
    worker = InventoryWorker(database, concurrency=max(1, args.concurrency))
    rows = unhealthy_sources(database, shard_index=args.shard_index, shard_count=args.shard_count)
    deadline = time.monotonic() + args.budget_seconds
    timeout = httpx.Timeout(float(os.getenv("GAIA_HTTP_TIMEOUT", "45")))
    limits = httpx.Limits(
        max_connections=max(16, args.concurrency * 3),
        max_keepalive_connections=max(8, args.concurrency * 2),
    )
    headers = {
        "User-Agent": os.getenv(
            "GAIA_USER_AGENT",
            "GAIA/5.0 source-by-source-inventory-audit (+github.com/catears124/GAIA)",
        ),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    results: list[AuditRow] = []
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for row in rows:
        queue.put_nowait(row)

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        headers=headers,
        follow_redirects=True,
    ) as client:
        async def consume() -> None:
            while time.monotonic() < deadline:
                try:
                    row = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    audited = await audit_one(
                        worker,
                        client,
                        row,
                        per_source_timeout=args.per_source_timeout,
                    )
                    results.append(audited)
                    print(json.dumps(asdict(audited), sort_keys=True), flush=True)
                finally:
                    queue.task_done()

        consumers = [asyncio.create_task(consume()) for _ in range(max(1, args.concurrency))]
        await asyncio.gather(*consumers)

    remaining = unhealthy_sources(database, shard_index=args.shard_index, shard_count=args.shard_count)
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
        "initial_unhealthy": len(rows),
        "audited": len(results),
        "probed": sum(item.outcome == "probed" for item in results),
        "leased_elsewhere": sum(item.outcome == "leased_elsewhere" for item in results),
        "timeouts": sum(item.outcome == "timeout" for item in results),
        "exceptions": sum(item.outcome == "exception" for item in results),
        "remaining_unhealthy": len(remaining),
        "not_reached": queue.qsize(),
        "results": [asdict(item) for item in sorted(results, key=lambda item: item.source)],
        "remaining_sources": [str(item["source"]) for item in remaining],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"results", "remaining_sources"}}, sort_keys=True))
    return report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m gaia.source_audit")
    root.add_argument("--shard-index", type=int, required=True)
    root.add_argument("--shard-count", type=int, required=True)
    root.add_argument("--concurrency", type=int, default=2)
    root.add_argument("--budget-seconds", type=float, default=2700)
    root.add_argument("--per-source-timeout", type=float, default=180)
    root.add_argument("--output", type=Path, required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        raise SystemExit("invalid shard index/count")
    logging.basicConfig(
        level=os.getenv("GAIA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_audit(args))


if __name__ == "__main__":
    main()
