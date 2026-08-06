from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .continuous_runtime_api import publish_committed_updates
from .maintenance_api import _claim_task, _finish_task, _request_allowed, _worker_id

_RUNTIME_DISCOVERY_TASK = "vercel-runtime-market-discovery"


async def run_runtime_market_discovery() -> dict[str, object]:
    """Continuously discover and validate new employer sources within a hard deadline."""
    from .dynamic_market_discovery import run_dynamic_market_discovery
    from .live_inventory import LiveDatabase

    database = LiveDatabase(migrate=False)
    worker_id = _worker_id("market-discovery")
    interval = max(
        900,
        int(os.getenv("GAIA_RUNTIME_MARKET_DISCOVERY_INTERVAL_SECONDS", "900")),
    )
    lease = max(
        120,
        min(
            int(os.getenv("GAIA_RUNTIME_MARKET_DISCOVERY_LEASE_SECONDS", "240")),
            300,
        ),
    )
    if not _claim_task(
        database,
        worker_id,
        task_key=_RUNTIME_DISCOVERY_TASK,
        lease_seconds=lease,
    ):
        return {"status": "not_due", "executed": False, "summary": None}

    probe_limit = max(
        1,
        min(int(os.getenv("GAIA_RUNTIME_MARKET_DISCOVERY_PROBE_LIMIT", "4")), 8),
    )
    concurrency = max(
        1,
        min(int(os.getenv("GAIA_RUNTIME_MARKET_DISCOVERY_CONCURRENCY", "4")), 6),
    )
    timeout = max(
        8.0,
        min(
            float(os.getenv("GAIA_RUNTIME_MARKET_DISCOVERY_TIMEOUT_SECONDS", "16")),
            18.0,
        ),
    )
    try:
        summary: dict[str, Any] = await asyncio.wait_for(
            run_dynamic_market_discovery(
                database,
                probe_limit=probe_limit,
                concurrency=concurrency,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        _finish_task(
            database,
            worker_id,
            task_key=_RUNTIME_DISCOVERY_TASK,
            interval_seconds=interval,
            status="partial",
            error=f"runtime market discovery exceeded {timeout:g} seconds",
        )
        published = await publish_committed_updates(database)
        return {
            "status": "partial",
            "executed": True,
            "summary": None,
            **published,
        }
    except Exception as error:  # noqa: BLE001 - isolate one discovery pulse.
        _finish_task(
            database,
            worker_id,
            task_key=_RUNTIME_DISCOVERY_TASK,
            interval_seconds=interval,
            status="broken",
            error=repr(error),
        )
        return {
            "status": "broken",
            "executed": True,
            "summary": None,
            "error": repr(error),
        }

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
    published = await publish_committed_updates(
        database,
        force_projection=promoted > 0,
    )
    return {
        "status": status,
        "executed": True,
        "summary": summary,
        **published,
    }


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
