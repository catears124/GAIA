from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request

_TICK_TASK = "vercel-runtime-inventory-tick"
_ALLOWED_USER_AGENTS = ("GAIA-production-maintenance/", "vercel-cron/1.0")


def _request_allowed(request: Request) -> bool:
    user_agent = request.headers.get("user-agent", "")
    return any(user_agent.startswith(prefix) for prefix in _ALLOWED_USER_AGENTS)


def _worker_id() -> str:
    deployment = os.getenv("VERCEL_DEPLOYMENT_ID") or os.getenv("VERCEL_URL")
    return f"vercel-tick:{deployment or socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _claim_tick(database: Any, worker_id: str) -> bool:
    interval = max(120, int(os.getenv("GAIA_RUNTIME_TICK_INTERVAL_SECONDS", "600")))
    lease = max(60, int(os.getenv("GAIA_RUNTIME_TICK_LEASE_SECONDS", "90")))
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO worker_tasks(task_key, next_run_at)
            VALUES (%s, now())
            ON CONFLICT(task_key) DO NOTHING
            """,
            (_TICK_TASK,),
        )
        row = connection.execute(
            """
            UPDATE worker_tasks
            SET lease_owner=%s,
                lease_expires_at=now() + (%s * interval '1 second'),
                last_started_at=now(),
                last_status='running',
                last_error=NULL,
                updated_at=now()
            WHERE task_key=%s
              AND next_run_at<=now()
              AND (lease_expires_at IS NULL OR lease_expires_at<now())
            RETURNING task_key
            """,
            (worker_id, lease, _TICK_TASK),
        ).fetchone()
    if row is None:
        return False
    # Reserve the next normal slot immediately. A failed run shortens this in _finish_tick.
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE worker_tasks
            SET next_run_at=now() + (%s * interval '1 second')
            WHERE task_key=%s AND lease_owner=%s
            """,
            (interval, _TICK_TASK, worker_id),
        )
    return True


def _finish_tick(
    database: Any,
    worker_id: str,
    *,
    status: str,
    error: str | None = None,
) -> None:
    retry = 60 if status == "broken" else max(
        120, int(os.getenv("GAIA_RUNTIME_TICK_INTERVAL_SECONDS", "600"))
    )
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE worker_tasks
            SET next_run_at=now() + (%s * interval '1 second'),
                lease_owner=NULL,
                lease_expires_at=NULL,
                last_finished_at=now(),
                last_status=%s,
                last_error=%s,
                updated_at=now()
            WHERE task_key=%s AND lease_owner=%s
            """,
            (retry, status, error, _TICK_TASK, worker_id),
        )


async def run_inventory_tick() -> dict[str, object]:
    """Run only due discovery/collector work, guarded by a database lease."""
    from .health import inventory_state
    from .live_inventory import InventoryWorker, LiveDatabase

    database = LiveDatabase(migrate=False)
    worker_id = _worker_id()
    if not _claim_tick(database, worker_id):
        inventory = inventory_state(database)
        return {
            "status": "not_due",
            "executed": False,
            "inventory": inventory,
            "summary": None,
        }

    budget = max(10.0, min(float(os.getenv("GAIA_RUNTIME_TICK_BUDGET_SECONDS", "42")), 48.0))
    concurrency = max(1, min(int(os.getenv("GAIA_RUNTIME_TICK_CONCURRENCY", "6")), 12))
    try:
        summary = await InventoryWorker(database, concurrency=concurrency).run(
            once=True,
            budget_seconds=budget,
        )
    except Exception as error:
        _finish_tick(database, worker_id, status="broken", error=repr(error))
        raise

    payload = summary.as_dict()
    status = "partial" if int(payload.get("failed") or 0) else "ok"
    _finish_tick(database, worker_id, status=status)
    inventory = inventory_state(database)
    return {
        "status": status,
        "executed": True,
        "inventory": inventory,
        "summary": payload,
    }


def install_maintenance_api(app: FastAPI) -> None:
    if getattr(app.state, "gaia_maintenance_api_installed", False):
        return
    app.state.gaia_maintenance_api_installed = True

    @app.post("/api/maintenance/tick", include_in_schema=False)
    async def maintenance_tick(request: Request) -> dict[str, object]:
        if os.getenv("GAIA_ENABLE_RUNTIME_TICK", "1") != "1":
            raise HTTPException(status_code=404, detail="runtime inventory tick disabled")
        if not _request_allowed(request):
            raise HTTPException(status_code=403, detail="maintenance caller not allowed")
        return await run_inventory_tick()
