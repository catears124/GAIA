from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute


SOURCE_DIAGNOSTICS_SQL = """
SELECT
    COALESCE(target.source, health.source) AS source,
    health.mode,
    health.complete,
    health.rows_scanned,
    health.expected_rows,
    health.target_rows,
    health.last_attempt_at,
    health.last_success_at,
    health.last_error,
    health.status,
    COALESCE(health.scope, catalog.scope) AS scope,
    health.note,
    health.last_run_id,
    health.lifecycle,
    health.consecutive_failures,
    catalog.kind AS catalog_kind,
    target.enabled,
    target.priority,
    target.interval_seconds,
    target.next_run_at,
    target.lease_expires_at,
    target.last_complete_at,
    target.last_status AS crawl_status,
    target.last_rows AS crawl_rows,
    target.consecutive_failures AS crawl_failures
FROM crawl_targets AS target
FULL OUTER JOIN source_health AS health USING(source)
LEFT JOIN source_catalog AS catalog
  ON catalog.source=COALESCE(target.source, health.source)
ORDER BY COALESCE(target.source, health.source)
"""


def _remove_legacy_coverage_route(app: FastAPI) -> None:
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            isinstance(route, APIRoute)
            and route.path == "/api/coverage"
            and "GET" in route.methods
        )
    ]


def install_coverage_api(app: FastAPI) -> None:
    """Install source diagnostics plus product-advancement health metrics."""
    if getattr(app.state, "gaia_coverage_api_installed", False):
        return
    app.state.gaia_coverage_api_installed = True
    _remove_legacy_coverage_route(app)

    @app.get("/api/coverage")
    def live_coverage() -> dict[str, object]:
        from . import api as legacy
        from .activity_metrics import (
            listing_freshness_state,
            source_growth_state,
            stall_assessment,
        )
        from .health import inventory_state

        data = legacy.db.coverage()
        with legacy.db.connect() as connection:
            rows = connection.execute(SOURCE_DIAGNOSTICS_SQL).fetchall()
        sources = [legacy.db._json_row(row) for row in rows]  # noqa: SLF001
        inventory = inventory_state(legacy.db)
        listing_freshness = listing_freshness_state(legacy.db)
        source_growth = source_growth_state(legacy.db)
        advancement = stall_assessment(listing_freshness, source_growth)
        contract = dict(data.get("contract") or {})
        contract.update(
            {
                "continuous_inventory": True,
                "configured_sources": int(inventory["total"]),
                "fresh_sources": int(inventory["fresh"]),
                "fresh_percent": inventory["fresh_percent"],
                "never_completed": int(inventory["never_completed"]),
                "overdue_sources": int(inventory["overdue"]),
                "degraded_sources": int(inventory["degraded"]),
                "historical_sources": int(inventory["historical"]),
                "coverage_watermark": inventory.get("coverage_watermark"),
                "source_candidates": int(source_growth["candidate_total"]),
                "due_source_candidates": int(source_growth["due"]),
                "new_unique_sources_24h": int(source_growth["new_unique_24h"]),
                "latest_unique_source_at": source_growth.get("latest_unique_source_at"),
                "newest_employer_posted_at": listing_freshness.get("newest_employer_posted_at"),
                "newest_found_at": listing_freshness.get("newest_found_at"),
                "newest_visible_activity_at": listing_freshness.get("newest_visible_activity_at"),
                "product_advancement_healthy": bool(advancement["healthy"]),
            }
        )
        data["contract"] = contract
        data["sources"] = sources
        data["listing_freshness"] = listing_freshness
        data["source_growth"] = source_growth
        data["product_advancement"] = advancement
        return data
