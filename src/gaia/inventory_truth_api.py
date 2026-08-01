from __future__ import annotations

import os
from typing import Any

from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from . import product_api


def _minimum(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def readiness_thresholds() -> dict[str, int]:
    return {
        "active_listings": _minimum("GAIA_BOOTSTRAP_MIN_ACTIVE_APPLICATIONS", 100),
        "companies": _minimum("GAIA_BOOTSTRAP_MIN_ACTIVE_COMPANIES", 20),
        "validated_sources": _minimum("GAIA_BOOTSTRAP_MIN_VALIDATED_SOURCES", 25),
    }


def classify_inventory(stats: dict[str, Any]) -> dict[str, Any]:
    thresholds = readiness_thresholds()
    observed = {key: int(stats.get(key) or 0) for key in thresholds}
    deficits = {key: max(0, thresholds[key] - observed[key]) for key in thresholds}
    complete = not any(deficits.values())
    ratios = [min(1.0, observed[key] / thresholds[key]) for key in thresholds]
    completion_percent = round(100.0 * min(ratios), 1)
    return {
        "complete": complete,
        "state": "ready" if complete else "recovering",
        "completion_percent": completion_percent,
        "observed": observed,
        "thresholds": thresholds,
        "deficits": deficits,
    }


def _remove_get_routes(app: Any, *paths: str) -> None:
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


def _stats_payload() -> dict[str, Any]:
    stats = dict(product_api.live_stats())
    recovery = classify_inventory(stats)
    stats["inventory_complete"] = recovery["complete"]
    stats["inventory_state"] = recovery["state"]
    stats["recovery"] = recovery
    return stats


def _recovery_response(payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Retry-After": "30",
        },
        content=payload,
    )


def install_inventory_truth_api(app: Any) -> None:
    """Make job-count completeness part of health and stats contracts."""

    _remove_get_routes(app, "/api/health", "/api/stats")

    @app.get("/api/stats", response_model=None)
    def truthful_stats() -> Any:
        stats = _stats_payload()
        if stats["inventory_complete"]:
            return stats
        return _recovery_response(stats)

    @app.get("/api/health", response_model=None)
    def truthful_health() -> Any:
        health = product_api.live_health()
        if not isinstance(health, dict):
            return health

        stats = _stats_payload()
        recovery = dict(stats["recovery"])
        health["job_inventory"] = recovery
        if recovery["complete"]:
            return health

        health["ok"] = False
        health["stale"] = True
        health["reason"] = "inventory_recovery"
        progress = dict(health.get("progress") or {})
        progress["stage"] = "inventory-recovery"
        progress["completed"] = int(recovery["observed"]["active_listings"])
        progress["total"] = int(recovery["thresholds"]["active_listings"])
        health["progress"] = progress
        data = dict(health.get("data") or {})
        last_run = dict(data.get("last_run") or {})
        last_run["status"] = "partial"
        data["last_run"] = last_run
        health["data"] = data
        return _recovery_response(health)
