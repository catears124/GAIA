from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request

from .database_scheduler import scheduler_status
from .db import Database
from .discord_notify import send_notifications
from .maintenance_api import _request_allowed, run_inventory_tick
from .runtime_secrets import resolved_runtime_secret, sync_runtime_secrets

_LOCK = asyncio.Lock()
_RUNTIME_SECRETS_SYNCED = False


@contextmanager
def _runtime_discord_environment(database: Database) -> Iterator[dict[str, bool]]:
    global _RUNTIME_SECRETS_SYNCED
    names = ("VERIFIED_DHOOK", "LEADS_DHOOK")
    previous = {name: os.environ.get(name) for name in names}
    previous_limit = os.environ.get("GAIA_DISCORD_MAX_PER_CHANNEL")
    resolved: dict[str, bool] = {}
    try:
        if not _RUNTIME_SECRETS_SYNCED and any(
            os.getenv(name, "").strip() for name in names
        ):
            sync_runtime_secrets(database)
            _RUNTIME_SECRETS_SYNCED = True
        for name in names:
            value = resolved_runtime_secret(database, name)
            resolved[name] = bool(value)
            if value:
                os.environ[name] = value
        os.environ["GAIA_DISCORD_MAX_PER_CHANNEL"] = os.getenv(
            "GAIA_RUNTIME_DISCORD_MAX_PER_CHANNEL",
            "10",
        )
        yield resolved
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if previous_limit is None:
            os.environ.pop("GAIA_DISCORD_MAX_PER_CHANNEL", None)
        else:
            os.environ["GAIA_DISCORD_MAX_PER_CHANNEL"] = previous_limit


def continuous_status(database: Database | None = None) -> dict[str, Any]:
    database = database or Database(migrate=False)
    scheduler = scheduler_status(database)
    webhooks = {
        name: bool(resolved_runtime_secret(database, name))
        for name in ("VERIFIED_DHOOK", "LEADS_DHOOK")
    }
    tick: dict[str, Any] = {}
    channels: list[dict[str, Any]] = []
    try:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT task_key, next_run_at, last_started_at, last_finished_at,
                       last_status, last_error, updated_at
                FROM worker_tasks
                WHERE task_key='vercel-runtime-inventory-tick'
                """
            ).fetchone()
            tick = dict(row or {})
            rows = connection.execute(
                """
                SELECT channel, initialized_at, updated_at,
                       (
                           SELECT COUNT(*)
                           FROM discord_notification_deliveries AS delivery
                           WHERE delivery.channel=channel_state.channel
                             AND delivery.disposition='sent'
                       ) AS sent_total,
                       (
                           SELECT MAX(delivered_at)
                           FROM discord_notification_deliveries AS delivery
                           WHERE delivery.channel=channel_state.channel
                             AND delivery.disposition='sent'
                       ) AS last_sent_at
                FROM discord_notification_channels AS channel_state
                ORDER BY channel
                """
            ).fetchall()
            channels = [dict(item) for item in rows]
    except Exception as error:  # noqa: BLE001 - status must remain available during recovery.
        tick = {"error": repr(error)}
    return {
        "scheduler": scheduler,
        "webhooks_configured": webhooks,
        "inventory_tick": tick,
        "discord_channels": channels,
    }


async def run_continuous_runtime_tick() -> dict[str, Any]:
    async with _LOCK:
        inventory = await run_inventory_tick()
        database = Database(migrate=False)
        notification_result: dict[str, object] | None = None
        notification_error: str | None = None
        with _runtime_discord_environment(database) as configured:
            if all(configured.values()):
                try:
                    notification_result = await asyncio.wait_for(
                        asyncio.to_thread(send_notifications, database),
                        timeout=35.0,
                    )
                except TimeoutError:
                    notification_error = "runtime Discord drain exceeded 35 seconds"
                except Exception as error:  # noqa: BLE001 - cron retries next minute.
                    notification_error = repr(error)
            else:
                missing = sorted(name for name, ready in configured.items() if not ready)
                notification_error = f"runtime Discord secrets missing: {', '.join(missing)}"
        return {
            "status": inventory.get("status"),
            "inventory": inventory,
            "notifications": notification_result,
            "notification_error": notification_error,
        }


def install_continuous_runtime_api(app: FastAPI) -> None:
    if getattr(app.state, "gaia_continuous_runtime_api_installed", False):
        return
    app.state.gaia_continuous_runtime_api_installed = True

    @app.get("/api/continuous-status", include_in_schema=False)
    async def public_continuous_status() -> dict[str, Any]:
        return await asyncio.to_thread(continuous_status)

    @app.post("/api/maintenance/continuous-tick", include_in_schema=False)
    async def continuous_runtime_tick(request: Request) -> dict[str, Any]:
        if os.getenv("GAIA_ENABLE_RUNTIME_TICK", "1") != "1":
            raise HTTPException(status_code=404, detail="runtime inventory tick disabled")
        if not _request_allowed(request):
            raise HTTPException(status_code=403, detail="maintenance caller not allowed")
        return await run_continuous_runtime_tick()
