from __future__ import annotations

import os
from datetime import datetime

import psycopg
from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from . import api as legacy
from .db import Database
from .health import inventory_state
from .universe import universe_summary

app = legacy.app


# Employer dates with day-level precision do not have a trustworthy time-of-day.
# Treat them as midnight rather than letting an invented hidden hour control feed order.
_POSTED_ACTIVITY_SQL = (
    "CASE "
    "WHEN latest_posted_at IS NULL THEN '-infinity'::timestamptz "
    "WHEN posted_precision='timestamp' THEN latest_posted_at "
    "ELSE date_trunc('day', latest_posted_at) END"
)
_FOUND_ACTIVITY_SQL = "date_trunc('hour', first_detected_at)"


def _live_order_clause(sort: str) -> str:
    if sort == "company":
        return "lower(company), lower(title), family_key"
    if sort == "verified":
        return "last_verified_at DESC, first_detected_at DESC, family_key"

    # Rank by the same precision the user can actually see. Recovery discoveries are
    # shown by hour, and day-only employer dates cannot legitimately win on a hidden
    # time-of-day. Exact timestamps still retain their full employer-provided precision.
    return (
        f"GREATEST({_FOUND_ACTIVITY_SQL}, {_POSTED_ACTIVITY_SQL}) DESC, "
        f"{_FOUND_ACTIVITY_SQL} DESC, "
        f"{_POSTED_ACTIVITY_SQL} DESC, "
        "CASE posted_precision WHEN 'timestamp' THEN 0 WHEN 'day' THEN 1 ELSE 2 END, "
        "first_detected_at DESC, latest_posted_at DESC NULLS LAST, "
        "last_verified_at DESC, family_key"
    )


def _normalize_visible_posted_time(item: dict[str, object]) -> None:
    """Remove invented time-of-day from imprecise employer dates in API responses."""
    value = item.get("latest_posted_at")
    if not value or item.get("posted_precision") == "timestamp":
        return
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return
    item["latest_posted_at"] = parsed.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).isoformat()


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


def _health_database() -> Database:
    """Use a short, isolated timeout so health probes never inherit worker deadlines."""
    database = Database(url=legacy.db.url, schema=legacy.db.schema, migrate=False)
    configured = int(float(os.getenv("GAIA_HEALTH_DB_TIMEOUT", "4")))
    database.timeout = max(1, min(configured, 10))
    return database


def _database_unavailable(error: Exception) -> JSONResponse:
    """Return truthful, cache-resistant outage evidence instead of a hanging 500."""
    return JSONResponse(
        status_code=503,
        headers={"Cache-Control": "no-store, max-age=0"},
        content={
            "ok": False,
            "read_only": True,
            "running": False,
            "stale": True,
            "reason": "database_unavailable",
            "detail": type(error).__name__,
            "progress": {
                "mode": "continuous-inventory",
                "stage": "database-recovery",
                "completed": 0,
                "total": 0,
                "current": None,
                "started_at": None,
                "elapsed_seconds": 0,
            },
            "last_summary": None,
            "data": {
                "last_run": None,
                "last_success_at": None,
                "sources": 0,
                "failing_sources": 0,
            },
            "inventory": {
                "total": 0,
                "fresh": 0,
                "unhealthy": 0,
                "running": 0,
                "never_completed": 0,
                "overdue": 0,
                "degraded": 0,
                "healthy": False,
                "fresh_percent": 0.0,
            },
        },
    )


legacy._order_clause = _live_order_clause
_remove_get_routes("/api/health", "/api/stats", "/api/families", "/api/facets")


@app.get("/api/health")
def live_health() -> dict[str, object] | JSONResponse:
    try:
        inventory = inventory_state(_health_database())
    except (psycopg.Error, OSError, TimeoutError, RuntimeError) as error:
        return _database_unavailable(error)
    fully_initialized = int(inventory["never_completed"]) == 0 and int(inventory["total"]) > 0
    watermark = inventory.get("coverage_watermark") if fully_initialized else None
    failing = int(inventory["unhealthy"])
    running = int(inventory["running"]) > 0
    return {
        "ok": bool(inventory["healthy"]),
        "read_only": os.getenv("GAIA_READ_ONLY", "0") == "1",
        "running": running,
        "stale": False,
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


@app.get("/api/families")
def live_families(
    q: str = Query("", max_length=200),
    category: str = "",
    target: str = "",
    track: str = "tech",
    trust: str = "all",
    location: str = Query("", max_length=100),
    sort: str = "newest",
    page: int = Query(1, ge=1),
    page_size: int = Query(48, ge=12, le=100),
    company: str = Query("", max_length=100),
    remote: bool = False,
    posted_within: int = Query(0, ge=0, le=365),
) -> dict[str, object]:
    trust = trust.strip() or "all"
    if trust not in {"verified", "leads", "all"}:
        raise HTTPException(status_code=400, detail="trust must be verified, leads, or all")
    if sort not in {"newest", "verified", "company"}:
        raise HTTPException(status_code=400, detail="sort must be newest, verified, or company")
    payload = legacy._list_families(
        query=q.strip(),
        category=category.strip(),
        target=target.strip(),
        track=track.strip(),
        trust=trust,
        location=location.strip(),
        sort=sort,
        page=page,
        page_size=page_size,
        company=company.strip(),
        remote=remote,
        posted_within=posted_within,
    )
    for raw_item in payload.get("items", []):
        if isinstance(raw_item, dict):
            _normalize_visible_posted_time(raw_item)
    return payload


@app.get("/api/facets")
def live_facets(trust: str = "all", target: str = "") -> dict[str, object]:
    return legacy.facets(trust=trust, target=target)


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
                    WHERE first_detected_at >= now() - interval '24 hours'
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
                    WHERE first_seen_at >= now() - interval '24 hours'
                ) AS new_today,
                COUNT(DISTINCT canonical_apply_url) FILTER (
                    WHERE removed_at >= now() - interval '24 hours'
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