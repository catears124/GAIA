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
    """Return only the cheap counters needed to prove conversion.

    The full conversion report scans postings, snapshots, tasks, and samples. Calling it
    before every candidate meant the repair endpoint often timed out before leasing a
    single source. Families is the small serving table and directly represents visible
    verified jobs, so use it as the transaction boundary for this hot path.
    """
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

    probe_limit = max(1, min(int(limit), 64))
    workers = max(1, min(int(concurrency), 12))
    database = LiveDatabase(migrate=False)
    before = _verified_snapshot(database, hours=hours)
    before_jobs = int(before.get("new_verified_jobs_window") or 0)

    candidate_lease = max(
        60,
        min(int(os.getenv("GAIA_DIAGNOSTIC_CANDIDATE_LEASE_SECONDS", "120")), 300),
    )
    worker = InventoryWorker(database, concurrency=workers)
    worker.lease_seconds = candidate_lease

    # Do not rebuild the entire serving model before doing useful work. Candidate probes
    # persist real employer-board results directly; rebuild once, and only if at least one
    # source produced a valid result.
    claimed_targets = _claim_fast_candidates(
        worker, limit=probe_limit, lease_seconds=candidate_lease
    )
    async with _client(workers) as client:
        promoted = sum(
            await asyncio.gather(
                *(worker._probe_candidate(client, target) for target in claimed_targets)
            )
        ) if claimed_targets else 0

    if promoted:
        worker.store.sync_catalog()
        database.rebuild_families()

    after = _verified_snapshot(database, hours=hours)
    after_jobs = int(after.get("new_verified_jobs_window") or 0)
    return {
        "status": "ok",
        "candidate_strategy": "curated_high_evidence_ats_first",
        "candidate_lease_seconds": candidate_lease,
        "claimed_candidates": len(claimed_targets),
        "promoted_sources": promoted,
        "verified_jobs_delta": after_jobs - before_jobs,
        "before": before,
        "after": after,
    }
