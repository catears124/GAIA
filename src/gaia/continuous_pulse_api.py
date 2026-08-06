from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .discord_notify import send_notifications
from .maintenance_api import _request_allowed, run_inventory_tick


def _notification_limit() -> int:
    raw = os.getenv("GAIA_VERCEL_DISCORD_MAX_PER_CHANNEL", "5")
    try:
        return max(1, min(int(raw), 20))
    except ValueError:
        return 5


def _drain_notifications() -> dict[str, object]:
    # A serverless cron invocation has a fixed deadline. Bound each drain while the
    # persistent delivery table guarantees that any remainder is picked up next minute.
    previous = os.environ.get("GAIA_DISCORD_MAX_PER_CHANNEL")
    os.environ["GAIA_DISCORD_MAX_PER_CHANNEL"] = str(_notification_limit())
    try:
        return send_notifications()
    finally:
        if previous is None:
            os.environ.pop("GAIA_DISCORD_MAX_PER_CHANNEL", None)
        else:
            os.environ["GAIA_DISCORD_MAX_PER_CHANNEL"] = previous


async def run_continuous_pulse() -> dict[str, Any]:
    inventory = await run_inventory_tick()
    notifications: dict[str, object] | None = None
    notification_error: str | None = None
    if os.getenv("VERIFIED_DHOOK", "").strip() or os.getenv("LEADS_DHOOK", "").strip():
        try:
            notifications = await asyncio.wait_for(
                asyncio.to_thread(_drain_notifications),
                timeout=35.0,
            )
        except TimeoutError:
            notification_error = "Discord drain exceeded the serverless deadline"
        except Exception as error:  # noqa: BLE001 - return evidence and retry next minute.
            notification_error = repr(error)
    else:
        notification_error = "Discord webhook environment variables are not configured on Vercel"

    return {
        "status": inventory.get("status"),
        "inventory": inventory,
        "notifications": notifications,
        "notification_error": notification_error,
    }


def install_continuous_pulse_api(app: FastAPI) -> None:
    if getattr(app.state, "gaia_continuous_pulse_installed", False):
        return
    app.state.gaia_continuous_pulse_installed = True

    @app.get("/api/maintenance/continuous-pulse", include_in_schema=False)
    async def continuous_pulse(request: Request) -> dict[str, Any]:
        if not _request_allowed(request):
            raise HTTPException(status_code=403, detail="maintenance caller not allowed")
        return await run_continuous_pulse()
