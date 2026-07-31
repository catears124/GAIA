from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

import httpx
from fastapi import FastAPI

from .live_inventory import InventoryWorker, LiveDatabase

LOGGER = logging.getLogger("gaia.runtime.bootstrap")
_BOOTSTRAP_TASK = "empty-database-bootstrap"


def _worker_id() -> str:
    deployment = os.getenv("VERCEL_DEPLOYMENT_ID") or os.getenv("VERCEL_URL")
    return f"vercel-bootstrap:{deployment or socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _claim_empty_database_bootstrap(database: LiveDatabase, worker_id: str) -> bool:
    """Lease one recovery attempt across every concurrent serverless instance."""
    lease_seconds = max(60, int(os.getenv("GAIA_BOOTSTRAP_LEASE_SECONDS", "180")))
    with database.connect() as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM postings) AS postings,
                (SELECT COUNT(*) FROM families) AS families
            """
        ).fetchone()
        if int(counts["postings"] or 0) > 0 or int(counts["families"] or 0) > 0:
            return False
        connection.execute(
            """
            INSERT INTO worker_tasks(task_key, next_run_at)
            VALUES (%s, now())
            ON CONFLICT(task_key) DO NOTHING
            """,
            (_BOOTSTRAP_TASK,),
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
            (worker_id, lease_seconds, _BOOTSTRAP_TASK),
        ).fetchone()
    return row is not None


def _finish_empty_database_bootstrap(
    database: LiveDatabase,
    worker_id: str,
    *,
    status: str,
    error: str | None,
) -> None:
    retry_seconds = 300 if status != "ok" else 6 * 3600
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
            (retry_seconds, status, error, _BOOTSTRAP_TASK, worker_id),
        )


async def bootstrap_empty_database() -> None:
    """Initialize and recover an empty project within one Vercel invocation."""
    if os.getenv("GAIA_BOOTSTRAP_EMPTY_DATABASE", "1") != "1":
        return

    database = LiveDatabase(migrate=False)
    try:
        # Vercel build imports never execute startup handlers. This is the first safe
        # place to perform real network I/O with the runtime Supabase credentials.
        await asyncio.to_thread(database.migrate)
    except Exception:
        LOGGER.exception("could not initialize the recreated Supabase schema")
        return

    worker_id = _worker_id()
    try:
        claimed = _claim_empty_database_bootstrap(database, worker_id)
    except Exception:
        LOGGER.exception("could not inspect the recreated database")
        return
    if not claimed:
        return

    budget = max(10.0, min(float(os.getenv("GAIA_BOOTSTRAP_BUDGET_SECONDS", "38")), 45.0))
    probe_limit = os.environ.setdefault("GAIA_CANDIDATE_PROBE_LIMIT", "6")
    worker = InventoryWorker(database, concurrency=max(2, int(probe_limit)))
    error: str | None = None
    status = "ok"
    try:
        timeout = httpx.Timeout(12.0, connect=6.0)
        limits = httpx.Limits(max_connections=24, max_keepalive_connections=12)
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            headers={
                "User-Agent": "GAIA/5.0 empty-database-recovery",
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            },
        ) as client:
            await asyncio.wait_for(
                worker._refresh_market(client, include_universe=False),  # noqa: SLF001
                timeout=budget,
            )
    except TimeoutError:
        status = "partial"
        error = f"bootstrap exceeded {budget:.0f}s budget"
        LOGGER.warning(error)
    except Exception as exc:
        status = "broken"
        error = repr(exc)
        LOGGER.exception("empty database bootstrap failed")
    finally:
        try:
            database.rebuild_families()
        except Exception as exc:
            status = "broken"
            error = error or repr(exc)
            LOGGER.exception("could not materialize bootstrap inventory")
        try:
            _finish_empty_database_bootstrap(
                database,
                worker_id,
                status=status,
                error=error,
            )
        except Exception:
            LOGGER.exception("could not release empty database bootstrap lease")


def install_runtime_bootstrap(app: FastAPI) -> None:
    """Run the bounded recovery only on a genuinely empty production database."""
    if getattr(app.state, "gaia_runtime_bootstrap_installed", False):
        return
    app.state.gaia_runtime_bootstrap_installed = True

    async def startup() -> None:
        try:
            await bootstrap_empty_database()
        except Exception:
            LOGGER.exception("runtime bootstrap crashed")

    app.add_event_handler("startup", startup)
