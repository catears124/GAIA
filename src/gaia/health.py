from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .db import Database
from .db_base import iso
from .universe import universe_summary

BAD_STATUSES = ("broken", "blocked", "truncated", "partial")


FRESHNESS_FLOOR_SECONDS = 90 * 60
FRESHNESS_INTERVAL_MULTIPLIER = 3


def inventory_state(database: Database) -> dict[str, Any]:
    """Return mutually exclusive source-health counts for the public inventory."""
    with database.connect() as connection:
        row = connection.execute(
            """
            WITH current_targets AS (
                SELECT
                    target.*,
                    GREATEST(target.interval_seconds * %s, %s) AS freshness_seconds
                FROM crawl_targets AS target
                JOIN source_catalog AS catalog USING(source)
                WHERE target.enabled
                  AND target.scheduled
                  AND catalog.validated
                  AND catalog.scope='current'
            )
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE target.lease_expires_at > now()
                ) AS running,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NULL
                ) AS never_completed,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NOT NULL
                      AND target.last_complete_at <
                          now() - make_interval(secs => target.freshness_seconds)
                ) AS overdue,
                COUNT(*) FILTER (
                    WHERE target.last_status = ANY(%s)
                ) AS degraded,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NOT NULL
                      AND target.last_complete_at >=
                          now() - make_interval(secs => target.freshness_seconds)
                      AND target.last_status <> ALL(%s)
                ) AS fresh,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NULL
                       OR target.last_complete_at <
                          now() - make_interval(secs => target.freshness_seconds)
                       OR target.last_status = ANY(%s)
                ) AS unhealthy,
                MAX(target.last_finished_at) AS latest_activity_at,
                MIN(target.last_complete_at) FILTER (
                    WHERE target.last_complete_at IS NOT NULL
                ) AS coverage_watermark
            FROM current_targets AS target
            """,
            (
                FRESHNESS_INTERVAL_MULTIPLIER,
                FRESHNESS_FLOOR_SECONDS,
                list(BAD_STATUSES),
                list(BAD_STATUSES),
                list(BAD_STATUSES),
            ),
        ).fetchone()
        historical = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM crawl_targets AS target
            JOIN source_catalog AS catalog USING(source)
            WHERE target.scheduled
              AND catalog.validated
              AND catalog.scope='historical'
            """
        ).fetchone()

    state: dict[str, Any] = {key: row[key] for key in row.keys()}
    for key in (
        "total",
        "running",
        "never_completed",
        "overdue",
        "degraded",
        "fresh",
        "unhealthy",
    ):
        state[key] = int(state.get(key) or 0)
    state["historical"] = int(historical["count"] or 0)
    state["latest_activity_at"] = iso(state.get("latest_activity_at"))
    state["coverage_watermark"] = iso(state.get("coverage_watermark"))
    state["freshness_floor_seconds"] = FRESHNESS_FLOOR_SECONDS
    total = int(state["total"])
    state["fresh_percent"] = round(100 * int(state["fresh"]) / total, 1) if total else 0.0
    state["healthy"] = bool(total) and int(state["unhealthy"]) == 0
    return state


def production_report(
    database: Database,
    *,
    max_activity_minutes: int = 90,
    min_sources: int = 100,
    min_active_listings: int = 25,
    require_healthy: bool = False,
    require_universe: bool = False,
) -> dict[str, Any]:
    """Evaluate whether hosted inventory is alive, populated, and advancing."""
    state = inventory_state(database)
    census = universe_summary(database, limit=1)
    with database.connect() as connection:
        product = connection.execute(
            """
            SELECT
                COUNT(*) AS active_families,
                COALESCE(SUM(direct_openings), 0) AS active_listings,
                COUNT(DISTINCT company) AS companies
            FROM families
            WHERE target_match!='not_internship'
              AND direct_openings>0
            """
        ).fetchone()
        snapshots = connection.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE finished_at >= now() - interval '30 minutes'
                ) AS recent_snapshots,
                COUNT(*) FILTER (
                    WHERE finished_at >= now() - interval '30 minutes'
                      AND status IN ('broken','blocked','truncated','partial')
                ) AS recent_failed_snapshots,
                MAX(finished_at) AS latest_snapshot_at
            FROM source_snapshots
            """
        ).fetchone()

    now = datetime.now(UTC)
    latest_raw = state.get("latest_activity_at")
    latest = datetime.fromisoformat(str(latest_raw)) if latest_raw else None
    activity_age_minutes = (
        max(0.0, (now - latest).total_seconds() / 60.0) if latest is not None else None
    )

    errors: list[str] = []
    warnings: list[str] = []
    total = int(state["total"])
    active_listings = int(product["active_listings"] or 0)
    census_summary = dict(census.get("summary") or {})
    if total < min_sources:
        errors.append(f"validated source count {total} is below floor {min_sources}")
    if active_listings < min_active_listings:
        errors.append(
            f"active direct listing count {active_listings} is below floor {min_active_listings}"
        )
    if activity_age_minutes is None:
        errors.append("inventory has no completed source activity")
    elif activity_age_minutes > max_activity_minutes:
        errors.append(
            f"inventory activity is {activity_age_minutes:.1f} minutes old; "
            f"maximum is {max_activity_minutes}"
        )
    if int(state["fresh"]) == 0:
        errors.append("no validated current source is fresh")
    if require_healthy and not bool(state["healthy"]):
        errors.append(f"{state['unhealthy']} sources are not current")
    elif not bool(state["healthy"]):
        warnings.append(
            f"inventory is catching up: {state['fresh']}/{state['total']} sources are fresh"
        )
    if int(state["degraded"]):
        warnings.append(f"{state['degraded']} sources have a degraded latest result")

    if require_universe:
        if not bool(census.get("ready")):
            errors.append("employer universe read model is missing")
        elif int(census_summary.get("known_employers") or 0) == 0:
            errors.append("employer universe contains no employers")
        elif int(census_summary.get("enumerated_employers") or 0) == 0:
            errors.append("employer universe contains no independently enumerated employers")
    elif not bool(census.get("ready")):
        warnings.append("employer universe read model has not been built")

    return {
        "ok": not errors,
        "checked_at": now.isoformat(),
        "activity_age_minutes": (
            round(activity_age_minutes, 1) if activity_age_minutes is not None else None
        ),
        "limits": {
            "max_activity_minutes": max_activity_minutes,
            "min_sources": min_sources,
            "min_active_listings": min_active_listings,
            "require_healthy": require_healthy,
            "require_universe": require_universe,
        },
        "inventory": state,
        "product": {
            "active_families": int(product["active_families"] or 0),
            "active_listings": active_listings,
            "companies": int(product["companies"] or 0),
        },
        "universe": census,
        "snapshots": {
            "recent_30m": int(snapshots["recent_snapshots"] or 0),
            "recent_failed_30m": int(snapshots["recent_failed_snapshots"] or 0),
            "latest_at": iso(snapshots["latest_snapshot_at"]),
        },
        "errors": errors,
        "warnings": warnings,
    }
