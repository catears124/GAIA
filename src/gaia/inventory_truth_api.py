from __future__ import annotations

import os
from typing import Any

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


def install_inventory_truth_api(app: Any) -> None:
    """Expose truthful live counts while distinguishing recovery from infrastructure outage.

    A reachable database with incomplete inventory is a degraded product state, not a 503.
    Returning live partial counts prevents the outage middleware from replacing fresher data
    with the older static snapshot while health still reports `ok=false`.
    """

    _remove_get_routes(app, "/api/health", "/api/stats")

    @app.get("/api/stats", response_model=None)
    def truthful_stats() -> Any:
        return _stats_payload()

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
        health["stale"] = False
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
        return health
