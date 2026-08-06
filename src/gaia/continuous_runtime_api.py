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
_FEED_PROJECTION_KEY = "public-families"
_FEED_PROJECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS gaia_feed_projection_state (
    projection_key TEXT PRIMARY KEY,
    projected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    latest_posting_at TIMESTAMPTZ,
    latest_removal_at TIMESTAMPTZ
);
ALTER TABLE gaia_feed_projection_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE gaia_feed_projection_state FROM anon, authenticated;
"""


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


def _feed_projection_snapshot(database: Database) -> dict[str, Any]:
    with database.connect() as connection:
        connection.execute(_FEED_PROJECTION_SCHEMA)
        current = connection.execute(
            """
            SELECT
                (
                    SELECT MAX(first_seen_at)
                    FROM postings
                    WHERE active AND target_match!='not_internship'
                ) AS latest_posting_at,
                (
                    SELECT MAX(finished_at)
                    FROM source_snapshots
                    WHERE removed_rows>0
                ) AS latest_removal_at
            """
        ).fetchone()
        projected = connection.execute(
            """
            SELECT projected_at, latest_posting_at, latest_removal_at
            FROM gaia_feed_projection_state
            WHERE projection_key=%s
            """,
            (_FEED_PROJECTION_KEY,),
        ).fetchone()

    current_row = dict(current or {})
    projected_row = dict(projected or {})
    current_posting = current_row.get("latest_posting_at")
    current_removal = current_row.get("latest_removal_at")
    projected_posting = projected_row.get("latest_posting_at")
    projected_removal = projected_row.get("latest_removal_at")
    posting_lag = current_posting is not None and (
        projected_posting is None or current_posting > projected_posting
    )
    removal_lag = current_removal is not None and (
        projected_removal is None or current_removal > projected_removal
    )
    return {
        "ready": bool(projected_row),
        "lagging": not projected_row or posting_lag or removal_lag,
        "projected_at": projected_row.get("projected_at"),
        "latest_posting_at": current_posting,
        "projected_posting_at": projected_posting,
        "latest_removal_at": current_removal,
        "projected_removal_at": projected_removal,
    }


def ensure_public_feed_current(
    database: Database,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Project committed postings into families even when a crawl batch is cancelled.

    The bounded inventory worker commits each completed source independently. Its old
    batch-level family rebuild was skipped whenever one slow source survived until the
    serverless deadline, leaving Discord current while the website stayed hours behind.
    Watermarks make the read model self-healing without rebuilding on every minute pulse.
    """
    before = _feed_projection_snapshot(database)
    if not force and not before["lagging"]:
        return {**before, "rebuilt": False, "busy": False}

    with database.connect() as lock_connection:
        lock_row = lock_connection.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
            ("gaia-public-feed-projection",),
        ).fetchone()
        acquired = bool(dict(lock_row or {}).get("acquired"))
        if not acquired:
            return {**before, "rebuilt": False, "busy": True}
        try:
            # Recheck after obtaining the cross-process lock. Another serverless pulse
            # may have repaired the feed while this invocation was waiting.
            locked = _feed_projection_snapshot(database)
            if force or locked["lagging"]:
                database.rebuild_families()
                latest = _feed_projection_snapshot(database)
                with database.connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO gaia_feed_projection_state(
                            projection_key, projected_at,
                            latest_posting_at, latest_removal_at
                        )
                        VALUES (%s, now(), %s, %s)
                        ON CONFLICT(projection_key) DO UPDATE SET
                            projected_at=now(),
                            latest_posting_at=EXCLUDED.latest_posting_at,
                            latest_removal_at=EXCLUDED.latest_removal_at
                        """,
                        (
                            _FEED_PROJECTION_KEY,
                            latest.get("latest_posting_at"),
                            latest.get("latest_removal_at"),
                        ),
                    )
                after = _feed_projection_snapshot(database)
                return {**after, "rebuilt": True, "busy": False}
            return {**locked, "rebuilt": False, "busy": False}
        finally:
            lock_connection.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))",
                ("gaia-public-feed-projection",),
            )


def continuous_status(database: Database | None = None) -> dict[str, Any]:
    database = database or Database(migrate=False)
    scheduler = scheduler_status(database)
    webhooks = {
        name: bool(resolved_runtime_secret(database, name))
        for name in ("VERIFIED_DHOOK", "LEADS_DHOOK")
    }
    tick: dict[str, Any] = {}
    channels: list[dict[str, Any]] = []
    feed_projection: dict[str, Any]
    try:
        feed_projection = _feed_projection_snapshot(database)
    except Exception as error:  # noqa: BLE001 - status must survive recovery failures.
        feed_projection = {"ready": False, "lagging": True, "error": repr(error)}
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
        "feed_projection": feed_projection,
        "discord_channels": channels,
    }


async def _repair_feed(
    database: Database,
    *,
    force: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        result = await asyncio.to_thread(
            ensure_public_feed_current,
            database,
            force=force,
        )
    except Exception as error:  # noqa: BLE001 - next minute pulse retries independently.
        return None, repr(error)
    return result, None


async def run_continuous_runtime_tick() -> dict[str, Any]:
    async with _LOCK:
        database = Database(migrate=False)

        # Repair any postings already committed by an earlier partial pulse before
        # spending this invocation's bounded crawl budget.
        projection, projection_error = await _repair_feed(database)

        inventory = await run_inventory_tick()
        summary = dict(inventory.get("summary") or {})
        changed = int(summary.get("new") or 0) + int(summary.get("removed") or 0) > 0
        post_projection, post_projection_error = await _repair_feed(
            database,
            force=changed,
        )
        if post_projection is not None:
            projection = post_projection
        if post_projection_error is not None:
            projection_error = post_projection_error

        notification_result: dict[str, object] | None = None
        notification_error: str | None = None
        with _runtime_discord_environment(database) as configured:
            if all(configured.values()):
                timeout = max(
                    2.0,
                    min(
                        float(os.getenv("GAIA_RUNTIME_DISCORD_TIMEOUT_SECONDS", "8")),
                        12.0,
                    ),
                )
                try:
                    notification_result = await asyncio.wait_for(
                        asyncio.to_thread(send_notifications, database),
                        timeout=timeout,
                    )
                except TimeoutError:
                    notification_error = (
                        f"runtime Discord drain exceeded {timeout:g} seconds; retrying next pulse"
                    )
                except Exception as error:  # noqa: BLE001 - cron retries next minute.
                    notification_error = repr(error)
            else:
                missing = sorted(name for name, ready in configured.items() if not ready)
                notification_error = f"runtime Discord secrets missing: {', '.join(missing)}"
        return {
            "status": inventory.get("status"),
            "inventory": inventory,
            "feed_projection": projection,
            "feed_projection_error": projection_error,
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
