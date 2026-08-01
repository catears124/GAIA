from __future__ import annotations

import os
from collections import Counter
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .maintenance_api import _request_allowed


def _rows(connection: Any, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    result = connection.execute(sql, params).fetchall()
    return [dict(row) for row in result]


def _safe_query(connection: Any, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    try:
        return _rows(connection, sql, params)
    except Exception as error:  # diagnostics must survive partial/older schemas
        return [{"diagnostic_error": repr(error)}]


def _failure_counts(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    counter: Counter[str] = Counter()
    for row in rows:
        if "diagnostic_error" in row:
            continue
        reason = (
            row.get("last_error")
            or row.get("rejection_reason")
            or row.get("last_status")
            or row.get("status")
            or "unknown"
        )
        counter[str(reason)[:240]] += 1
    return [
        {"reason": reason, "count": count}
        for reason, count in counter.most_common(25)
    ]


def install_conversion_diagnostics_api(app: FastAPI) -> None:
    """Install an authenticated funnel debugger for future repair runs.

    This endpoint intentionally returns concrete recent records, not only aggregate
    health. It lets an operator see why discovered sources and crawled jobs are not
    becoming validated, visible inventory without requiring direct database access.
    """
    if getattr(app.state, "gaia_conversion_diagnostics_installed", False):
        return
    app.state.gaia_conversion_diagnostics_installed = True

    @app.get("/api/maintenance/diagnostics/conversion", include_in_schema=False)
    def conversion_diagnostics(request: Request, limit: int = 50) -> dict[str, object]:
        if os.getenv("GAIA_ENABLE_CONVERSION_DIAGNOSTICS", "1") != "1":
            raise HTTPException(status_code=404, detail="conversion diagnostics disabled")
        if not _request_allowed(request):
            raise HTTPException(status_code=403, detail="maintenance caller not allowed")

        from . import api as legacy
        from .activity_metrics import listing_freshness_state, source_growth_state

        sample_limit = max(1, min(int(limit), 200))
        with legacy.db.connect() as connection:
            source_candidates = _safe_query(
                connection,
                """
                SELECT *
                FROM source_candidates
                WHERE status!='promoted'
                ORDER BY COALESCE(last_probe_at, last_seen_at, first_seen_at) DESC
                LIMIT %s
                """,
                (sample_limit,),
            )
            unhealthy_sources = _safe_query(
                connection,
                """
                SELECT COALESCE(h.source, t.source) AS source,
                       h.status, h.last_error, h.last_attempt_at, h.last_success_at,
                       h.consecutive_failures, t.last_status, t.last_rows,
                       t.next_run_at, t.lease_expires_at
                FROM source_health h
                FULL OUTER JOIN crawl_targets t USING(source)
                WHERE COALESCE(h.status, t.last_status, '') NOT IN ('ok', 'complete', 'healthy')
                   OR COALESCE(h.consecutive_failures, t.consecutive_failures, 0) > 0
                ORDER BY COALESCE(h.last_attempt_at, t.next_run_at) DESC NULLS LAST
                LIMIT %s
                """,
                (sample_limit,),
            )
            recent_families = _safe_query(
                connection,
                """
                SELECT id, company, title, target_match, direct_openings,
                       first_detected_at, latest_posted_at, updated_at
                FROM families
                ORDER BY COALESCE(first_detected_at, updated_at) DESC NULLS LAST
                LIMIT %s
                """,
                (sample_limit,),
            )
            hidden_or_rejected = _safe_query(
                connection,
                """
                SELECT id, company, title, target_match, direct_openings,
                       first_detected_at, latest_posted_at, updated_at
                FROM families
                WHERE target_match='not_internship' OR direct_openings<=0
                ORDER BY COALESCE(first_detected_at, updated_at) DESC NULLS LAST
                LIMIT %s
                """,
                (sample_limit,),
            )
            recent_runs = _safe_query(
                connection,
                """
                SELECT * FROM sync_runs
                ORDER BY COALESCE(finished_at, started_at) DESC
                LIMIT %s
                """,
                (sample_limit,),
            )

        listing = listing_freshness_state(legacy.db)
        growth = source_growth_state(legacy.db)
        verified_24h = int(listing.get("found_24h") or 0)
        discovered_24h = int(growth.get("new_unique_24h") or 0)
        return {
            "objective": "increase_net_new_verified_user_visible_jobs",
            "listing_freshness": listing,
            "source_growth": growth,
            "funnel": {
                "new_verified_jobs_24h": verified_24h,
                "new_source_evidence_24h": discovered_24h,
                "source_evidence_to_verified_job_ratio": (
                    round(verified_24h / discovered_24h, 4) if discovered_24h else None
                ),
            },
            "source_candidate_failure_counts": _failure_counts(source_candidates),
            "unhealthy_source_failure_counts": _failure_counts(unhealthy_sources),
            "source_candidates": source_candidates,
            "unhealthy_sources": unhealthy_sources,
            "recent_families": recent_families,
            "hidden_or_rejected_families": hidden_or_rejected,
            "recent_sync_runs": recent_runs,
            "sample_limit": sample_limit,
        }
