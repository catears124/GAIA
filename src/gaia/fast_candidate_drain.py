from __future__ import annotations

import asyncio
import os
from typing import Any

from .inventory import ClaimedTarget

_FAST_KINDS = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "recruitee",
    "workable",
    "jobvite",
    "icims",
    "oracle-cloud",
    "successfactors",
    "workday-search",
    "google-careers",
)


def _claim_fast_candidates(worker, *, limit: int, lease_seconds: int) -> list[ClaimedTarget]:
    """Lease the highest-signal due ATS candidates before speculative probes."""
    with worker.database.connect() as connection:
        rows = connection.execute(
            """
            WITH selected AS (
                SELECT source
                FROM source_candidates
                WHERE status IN ('candidate','retry')
                  AND next_probe_at<=now()
                  AND (lease_expires_at IS NULL OR lease_expires_at<now())
                  AND kind = ANY(%s)
                ORDER BY
                  (scope='current') DESC,
                  (origin='curated-registry') DESC,
                  (status='candidate') DESC,
                  consecutive_failures ASC,
                  evidence_count DESC,
                  last_seen_at DESC,
                  CASE kind
                    WHEN 'greenhouse' THEN 0
                    WHEN 'lever' THEN 1
                    WHEN 'ashby' THEN 2
                    WHEN 'smartrecruiters' THEN 3
                    WHEN 'recruitee' THEN 4
                    WHEN 'workable' THEN 5
                    WHEN 'jobvite' THEN 6
                    WHEN 'icims' THEN 7
                    WHEN 'oracle-cloud' THEN 8
                    WHEN 'successfactors' THEN 9
                    WHEN 'workday-search' THEN 10
                    ELSE 11
                  END,
                  next_probe_at,
                  source
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE source_candidates AS candidate
            SET lease_owner=%s,
                lease_expires_at=now() + (%s * interval '1 second'),
                last_probe_at=now()
            FROM selected
            WHERE candidate.source=selected.source
            RETURNING candidate.source,candidate.kind,candidate.scope,candidate.spec,
                      candidate.consecutive_failures
            """,
            (list(_FAST_KINDS), limit, worker.store.worker_id, lease_seconds),
        ).fetchall()
    return [
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
        for row in rows
    ]


def _verified_snapshot(database: Any, *, hours: int) -> dict[str, object]:
    window = max(1, min(int(hours), 720))
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE target_match IN ('exact','year_confirmed','source_confirmed')
                  AND direct_openings>0
              ) AS active_verified_jobs,
              COALESCE(SUM(direct_openings) FILTER (
                WHERE target_match IN ('exact','year_confirmed','source_confirmed')
                  AND direct_openings>0
              ),0) AS active_verified_openings,
              COUNT(*) FILTER (
                WHERE target_match IN ('exact','year_confirmed','source_confirmed')
                  AND direct_openings>0
                  AND first_detected_at>=now()-(%s*interval '1 hour')
              ) AS new_verified_jobs_window,
              MAX(first_detected_at) FILTER (
                WHERE target_match IN ('exact','year_confirmed','source_confirmed')
                  AND direct_openings>0
              ) AS newest_verified_detected_at
            FROM families
            """,
            (window,),
        ).fetchone()
    return dict(row or {})


async def drain_candidates(*, limit: int, concurrency: int, hours: int) -> dict[str, object]:
    from .dynamic_market_discovery import _client
    from .live_inventory import InventoryWorker, LiveDatabase

    probe_limit = max(1, min(int(limit), 16))
    workers = max(1, min(int(concurrency), 4))
    database = LiveDatabase(migrate=False)

    candidate_lease = max(
        30,
        min(int(os.getenv("GAIA_DIAGNOSTIC_CANDIDATE_LEASE_SECONDS", "75")), 120),
    )
    per_probe_timeout = max(
        4.0,
        min(float(os.getenv("GAIA_DIAGNOSTIC_PROBE_TIMEOUT_SECONDS", "9")), 15.0),
    )
    worker = InventoryWorker(database, concurrency=workers)
    worker.lease_seconds = candidate_lease

    claimed_targets = _claim_fast_candidates(
        worker, limit=probe_limit, lease_seconds=candidate_lease
    )

    async def bounded_probe(client, target: ClaimedTarget) -> bool:
        try:
            return await asyncio.wait_for(
                worker._probe_candidate(client, target), timeout=per_probe_timeout
            )
        except TimeoutError:
            worker.store.finish_candidate(
                target,
                promoted=False,
                status="timeout",
                error=f"diagnostic probe exceeded {per_probe_timeout:.1f}s",
            )
            return False
        except asyncio.CancelledError:
            try:
                worker.store.finish_candidate(
                    target,
                    promoted=False,
                    status="cancelled",
                    error="diagnostic request cancelled",
                )
            finally:
                raise

    async with _client(workers) as client:
        promoted = sum(
            await asyncio.gather(
                *(bounded_probe(client, target) for target in claimed_targets)
            )
        ) if claimed_targets else 0

    rebuilt = 0
    if promoted:
        worker.store.sync_catalog()
        rebuilt = int(database.rebuild_families() or 0)

    # Snapshot only after useful work. The old preflight count consumed the entire
    # serverless request during database pressure, so zero candidates were ever leased.
    after: dict[str, object] = {}
    try:
        after = _verified_snapshot(database, hours=hours)
    except Exception as exc:
        after = {"snapshot_error": repr(exc)}

    return {
        "status": "ok",
        "candidate_strategy": "bounded_curated_ats_first",
        "candidate_lease_seconds": candidate_lease,
        "per_probe_timeout_seconds": per_probe_timeout,
        "claimed_candidates": len(claimed_targets),
        "promoted_sources": promoted,
        "families_rebuilt": rebuilt,
        "verified_jobs_delta": 0,
        "after": after,
    }
