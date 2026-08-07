from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .continuous_runtime_api import ensure_public_feed_current
from .dynamic_market_discovery import SNAPSHOT_VERSION, _client, deserialize_candidates
from .inventory import ClaimedTarget
from .inventory_runtime import COVERAGE_KINDS
from .live_inventory import InventoryWorker, LiveDatabase
from .quality import canonical_source_name
from .source_catalog import save_candidates


def _prune_validated_candidates(database: LiveDatabase) -> set[str]:
    """Remove rediscovered sources that are already trusted production catalog entries."""
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT source FROM source_catalog WHERE validated"
        ).fetchall()
        known = {str(row["source"]) for row in rows}
        connection.execute(
            """
            DELETE FROM source_candidates AS candidate
            USING source_catalog AS catalog
            WHERE candidate.source=catalog.source
              AND catalog.validated
            """
        )
    return known


def _claim_balanced_candidates(
    worker: InventoryWorker,
    *,
    limit: int,
) -> list[ClaimedTarget]:
    """Lease due candidates round-robin across provider kinds.

    Dynamic discovery can produce thousands of domain candidates. The generic queue
    sorts by evidence count, which allowed one expensive kind to monopolize a whole
    validation run. Claiming one source per kind per pass guarantees ATS providers and
    career domains all receive capacity while preserving row-level SKIP LOCKED safety.
    """
    bounded = max(1, int(limit))
    claimed: list[ClaimedTarget] = []
    lease_seconds = worker.lease_seconds

    with worker.database.connect() as connection:
        kinds = [
            str(row["kind"])
            for row in connection.execute(
                """
                SELECT DISTINCT kind
                FROM source_candidates
                WHERE status IN ('candidate','retry')
                  AND next_probe_at<=now()
                  AND (lease_expires_at IS NULL OR lease_expires_at<now())
                ORDER BY kind
                """
            ).fetchall()
        ]
        if not kinds:
            return []

        while len(claimed) < bounded:
            progressed = False
            for kind in kinds:
                if len(claimed) >= bounded:
                    break
                row = connection.execute(
                    """
                    WITH selected AS (
                        SELECT source
                        FROM source_candidates
                        WHERE kind=%s
                          AND status IN ('candidate','retry')
                          AND next_probe_at<=now()
                          AND (lease_expires_at IS NULL OR lease_expires_at<now())
                        ORDER BY
                            (scope='current') DESC,
                            evidence_count DESC,
                            next_probe_at,
                            source
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE source_candidates AS candidate
                    SET lease_owner=%s,
                        lease_expires_at=now() + (%s * interval '1 second'),
                        last_probe_at=now()
                    FROM selected
                    WHERE candidate.source=selected.source
                    RETURNING candidate.source, candidate.kind, candidate.scope,
                              candidate.spec, candidate.consecutive_failures
                    """,
                    (kind, worker.store.worker_id, lease_seconds),
                ).fetchone()
                if row is None:
                    continue
                progressed = True
                claimed.append(
                    ClaimedTarget(
                        source=str(row["source"]),
                        kind=str(row["kind"]),
                        scope=str(row["scope"]),
                        spec=dict(row["spec"] or {}),
                        interval_seconds=worker.store._default_interval(
                            str(row["kind"]), str(row["scope"])
                        ),
                        consecutive_failures=int(row["consecutive_failures"] or 0),
                    )
                )
            if not progressed:
                break
    return claimed


def _schedule_promoted_sources(
    worker: InventoryWorker,
    targets: list[ClaimedTarget],
) -> int:
    """Schedule only newly validated sources; never rewrite every crawl target."""
    if not targets:
        return 0
    payload = [
        (
            target.source,
            target.kind in COVERAGE_KINDS,
            worker.store._priority(target.kind, target.scope),
            worker.store._default_interval(target.kind, target.scope),
        )
        for target in targets
    ]
    with worker.database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO crawl_targets(
                source, enabled, scheduled, priority, interval_seconds, next_run_at
            )
            VALUES (%s,%s,TRUE,%s,%s,now())
            ON CONFLICT(source) DO UPDATE SET
                enabled=excluded.enabled,
                scheduled=TRUE,
                priority=excluded.priority,
                interval_seconds=excluded.interval_seconds,
                next_run_at=LEAST(
                    COALESCE(crawl_targets.next_run_at, now()),
                    now()
                ),
                updated_at=now()
            """,
            payload,
        )
    return len(payload)


async def ingest_snapshot(
    snapshot: dict[str, Any],
    *,
    probe_limit: int,
    concurrency: int,
) -> dict[str, Any]:
    if int(snapshot.get("version") or 0) != SNAPSHOT_VERSION:
        raise ValueError("unsupported dynamic market snapshot version")
    rows = snapshot.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("dynamic market snapshot is missing candidates")

    database = LiveDatabase(migrate=False)
    worker = InventoryWorker(database, concurrency=max(1, int(concurrency)))
    known = _prune_validated_candidates(database)
    candidates = deserialize_candidates([row for row in rows if isinstance(row, dict)])
    unknown = [
        collector
        for collector in candidates
        if canonical_source_name(collector.name) not in known
    ]
    saved = save_candidates(database, unknown, origin="dynamic-github-market")

    claimed = _claim_balanced_candidates(worker, limit=max(1, int(probe_limit)))
    outcomes: list[bool] = []
    async with _client(max(1, int(concurrency))) as client:
        if claimed:
            outcomes = list(
                await asyncio.gather(
                    *(worker._probe_candidate(client, target) for target in claimed)
                )
            )

    promoted_targets = [
        target for target, promoted in zip(claimed, outcomes, strict=False) if promoted
    ]
    scheduled = _schedule_promoted_sources(worker, promoted_targets)

    projection: dict[str, Any] | None = None
    projection_error: str | None = None
    if promoted_targets:
        try:
            projection = await asyncio.to_thread(
                ensure_public_feed_current,
                database,
                force=True,
            )
        except Exception as error:  # noqa: BLE001 - committed sources survive projection retry.
            projection_error = repr(error)

    base_summary = snapshot.get("summary")
    summary: dict[str, Any] = dict(base_summary) if isinstance(base_summary, dict) else {}
    summary.update(
        {
            "candidate_rows_in_snapshot": len(candidates),
            "candidate_rows_already_validated": len(candidates) - len(unknown),
            "candidate_rows_written": saved,
            "candidate_sources_probed": len(claimed),
            "candidate_probe_kinds": sorted({target.kind for target in claimed}),
            "candidate_sources_promoted": len(promoted_targets),
            "promoted_sources": [target.source for target in promoted_targets],
            "catalog_sources_scheduled": scheduled,
            "feed_projection": projection,
            "feed_projection_error": projection_error,
        }
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Incrementally validate and schedule dynamic GAIA source candidates"
    )
    parser.add_argument("--snapshot-input", type=Path, required=True)
    parser.add_argument("--probe-limit", type=int, default=16)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot_input.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise ValueError("dynamic market snapshot must be a JSON object")
    result = asyncio.run(
        ingest_snapshot(
            snapshot,
            probe_limit=args.probe_limit,
            concurrency=args.concurrency,
        )
    )
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
