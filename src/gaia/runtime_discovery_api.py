from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .maintenance_api import _claim_task, _finish_task, _request_allowed, _worker_id

_RUNTIME_DISCOVERY_TASK = "vercel-runtime-market-discovery"


async def run_runtime_market_discovery() -> dict[str, object]:
    """Search public internship indexes and promote official employer sources in production."""
    from .dynamic_market_discovery import run_dynamic_market_discovery
    from .live_inventory import LiveDatabase

    database = LiveDatabase(migrate=False)
    worker_id = _worker_id("market-discovery")
    interval = max(
        300,
        int(os.getenv("GAIA_RUNTIME_MARKET_DISCOVERY_INTERVAL_SECONDS", "900")),
    )
    lease = max(
        120,
        int(os.getenv("GAIA_RUNTIME_MARKET_DISCOVERY_LEASE_SECONDS", "240")),
    )
    if not _claim_task(
        database,
        worker_id,
        task_key=_RUNTIME_DISCOVERY_TASK,
        interval_seconds=interval,
        lease_seconds=lease,
    ):
        return {"status": "not_due", "executed": False, "summary": None}

    probe_limit = max(
        1,
        min(int(os.getenv("GAIA_RUNTIME_MARKET_DISCOVERY_PROBE_LIMIT", "10")), 24),
    )
    concurrency = max(
        1,
        min(int(os.getenv("GAIA_RUNTIME_MARKET_DISCOVERY_CONCURRENCY", "6")), 10),
    )
    try:
        summary: dict[str, Any] = await run_dynamic_market_discovery(
            database,
            probe_limit=probe_limit,
            concurrency=concurrency,
        )
    except Exception as error:
        _finish_task(
            database,
            worker_id,
            task_key=_RUNTIME_DISCOVERY_TASK,
            interval_seconds=interval,
            status="broken",
            error=repr(error),
        )
        raise

    promoted = int(summary.get("candidate_sources_promoted") or 0)
    saved = int(summary.get("candidate_rows_written") or 0)
    status = "ok" if promoted or saved else "empty"
    _finish_task(
        database,
        worker_id,
        task_key=_RUNTIME_DISCOVERY_TASK,
        interval_seconds=interval,
        status=status,
    )
    return {"status": status, "executed": True, "summary": summary}


def install_runtime_discovery_api(app: FastAPI) -> None:
    if getattr(app.state, "gaia_runtime_discovery_api_installed", False):
        return
    app.state.gaia_runtime_discovery_api_installed = True

    @app.post("/api/maintenance/discover", include_in_schema=False)
    async def runtime_market_discovery(request: Request) -> dict[str, object]:
        if os.getenv("GAIA_ENABLE_RUNTIME_MARKET_DISCOVERY", "1") != "1":
            raise HTTPException(status_code=404, detail="runtime market discovery disabled")
        if not _request_allowed(request):
            raise HTTPException(status_code=403, detail="maintenance caller not allowed")
        return await run_runtime_market_discovery()
