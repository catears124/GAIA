from __future__ import annotations

import os

from fastapi.routing import APIRoute

from . import api as legacy
from .health import inventory_state
from .universe import universe_summary

app = legacy.app


def _live_order_clause(sort: str) -> str:
    if sort == "company":
        return "lower(company), lower(title), family_key"
    if sort == "verified":
        return "last_verified_at DESC, first_detected_at DESC, family_key"
    # Recent means the newest meaningful event. Employer-published dates are preferred,
    # higher-precision dates break ties, and GAIA's first-detected time is the fallback.
    return (
        "COALESCE(latest_posted_at, first_detected_at) DESC, "
        "(latest_posted_at IS NOT NULL) DESC, "
        "CASE posted_precision WHEN 'timestamp' THEN 0 WHEN 'day' THEN 1 ELSE 2 END, "
        "first_detected_at DESC, last_verified_at DESC, family_key"
    )


def _remove_get_routes(*paths: str) -> None:
    removed = set(paths)
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            isinstance(route, APIRoute)
            and route.path in removed
            and "GET" in route.methods
        )
    ]


legacy._order_clause = _live_order_clause
_remove_get_routes("/api/health", "/api/stats")


@app.get("/api/health")
def live_health() -> dict[str, object]:
    inventory = inventory_state(legacy.db)
    fully_initialized = int(inventory["never_completed"]) == 0 and int(inventory["total"]) > 0
    watermark = inventory.get("coverage_watermark") if fully_initialized else None
    failing = int(inventory["unhealthy"])
    running = int(inventory["running"]) > 0
    return {
        "ok": bool(inventory["healthy"]),
        "read_only": os.getenv("GAIA_READ_ONLY", "0") == "1",
        "running": running,
        "progress": {
            "mode": "continuous-inventory",
            "stage": "crawling" if running else "scheduled",
            "completed": int(inventory["fresh"]),
            "total": int(inventory["total"]),
            "current": None,
            "started_at": None,
            "elapsed_seconds": 0,
        },
        "last_summary": None,
        "data": {
            "last_run": (
                {
                    "finished_at": watermark,
                    "status": "ok" if inventory["healthy"] else "degraded",
                }
                if watermark
                else None
            ),
            "last_success_at": watermark,
            "sources": int(inventory["total"]),
            "failing_sources": failing,
        },
        "inventory": inventory,
    }


@app.get("/api/stats")
def live_stats() -> dict[str, object]:
    with legacy.db.connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS role_families,
                COALESCE(SUM(direct_openings), 0) AS active_listings,
                COUNT(DISTINCT company) AS companies,
                COUNT(*) FILTER (
                    WHERE first_detected_at >= date_trunc('day', now())
                ) AS new_families_today,
                COALESCE(SUM(direct_openings), 0) AS verified_listings,
                COUNT(*) AS verified_families
            FROM families
            WHERE target_match!='not_internship'
              AND category = ANY(%s)
              AND direct_openings>0
            """,
            (list(legacy.TECH_CATEGORIES),),
        ).fetchone()
        lead_row = connection.execute(
            """
            SELECT COUNT(*) AS leads, COALESCE(SUM(backstop_openings),0) AS lead_apps
            FROM families
            WHERE target_match!='not_internship'
              AND category = ANY(%s)
              AND direct_openings=0
              AND backstop_openings>0
            """,
            (list(legacy.TECH_CATEGORIES),),
        ).fetchone()
        movement = connection.execute(
            """
            SELECT
                COUNT(DISTINCT canonical_apply_url) FILTER (
                    WHERE first_seen_at >= date_trunc('day', now())
                ) AS new_today,
                COUNT(DISTINCT canonical_apply_url) FILTER (
                    WHERE removed_at >= date_trunc('day', now())
                ) AS removed_today
            FROM postings
            WHERE source_mode='direct'
              AND target_match!='not_internship'
              AND category = ANY(%s)
            """,
            (list(legacy.TECH_CATEGORIES),),
        ).fetchone()
        source_row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM crawl_targets AS target
            JOIN source_catalog AS catalog USING(source)
            WHERE target.enabled
              AND target.scheduled
              AND catalog.validated
              AND catalog.scope='current'
            """
        ).fetchone()

    census = universe_summary(legacy.db, limit=1)
    census_summary = dict(census.get("summary") or {})
    new_today = int(movement["new_today"] or 0)
    removed_today = int(movement["removed_today"] or 0)
    return {
        "role_families": int(row["role_families"]),
        "active_listings": int(row["active_listings"]),
        "companies": int(row["companies"]),
        "new_24h": new_today,
        "new_today": new_today,
        "removed_today": removed_today,
        "net_today": new_today - removed_today,
        "new_families_24h": int(row["new_families_today"]),
        "verified_listings": int(row["verified_listings"]),
        "verified_families": int(row["verified_families"]),
        "validated_sources": int(source_row["count"] or 0),
        "known_employers": int(census_summary.get("known_employers") or 0),
        "enumerated_employers": int(census_summary.get("enumerated_employers") or 0),
        "unresolved_employers": int(census_summary.get("unresolved_employers") or 0),
        "blind_spots": int(census_summary.get("blind_spots") or 0),
        "leads": int(lead_row["leads"]),
        "lead_apps": int(lead_row["lead_apps"]),
    }


@app.get("/api/universe")
def employer_universe(limit: int = 80) -> dict[str, object]:
    return universe_summary(legacy.db, limit=max(1, min(limit, 250)))
