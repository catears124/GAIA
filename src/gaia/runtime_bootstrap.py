from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

import httpx

from .live_inventory import InventoryWorker, LiveDatabase

LOGGER = logging.getLogger("gaia.runtime.bootstrap")
_BOOTSTRAP_TASK = "empty-database-bootstrap"


def _worker_id() -> str:
    deployment = os.getenv("VERCEL_DEPLOYMENT_ID") or os.getenv("VERCEL_URL")
    return f"vercel-bootstrap:{deployment or socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _initialize_schema(database: LiveDatabase) -> None:
    """Prefer the Supabase direct URL, then fall back to the transaction pooler."""
    direct_error: Exception | None = None
    try:
        from .cli import run_migration

        run_migration()
    except Exception as exc:
        direct_error = exc
        LOGGER.warning("direct Supabase migration failed; trying pooler: %r", exc)
        database.migrate()
        from .employer_census import ECOSYSTEM_SCHEMA_STATEMENTS
        from .universe import UNIVERSE_SCHEMA_STATEMENTS

        with database.connect() as connection:
            for statement in (*UNIVERSE_SCHEMA_STATEMENTS, *ECOSYSTEM_SCHEMA_STATEMENTS):
                connection.execute(statement)

    with database.connect() as connection:
        # Discovery is due immediately, but no source is called validated until its
        # collector completes a real employer-board enumeration.
        connection.execute(
            """
            INSERT INTO worker_tasks(task_key, next_run_at)
            VALUES
                ('market-discovery', now()),
                ('universe-discovery', now() + interval '1 hour'),
                (%s, now())
            ON CONFLICT(task_key) DO NOTHING
            """,
            (_BOOTSTRAP_TASK,),
        )
    if direct_error is not None:
        LOGGER.info("schema initialized successfully through the pooled fallback")


def _inventory_counts(database: LiveDatabase) -> dict[str, int]:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE target_match!='not_internship'
                      AND direct_openings>0
                ) AS active_families,
                COALESCE(SUM(direct_openings) FILTER (
                    WHERE target_match!='not_internship'
                ), 0) AS active_applications,
                COUNT(DISTINCT company) FILTER (
                    WHERE target_match!='not_internship'
                      AND direct_openings>0
                ) AS active_companies,
                (SELECT COUNT(*) FROM source_catalog WHERE validated) AS validated_sources
            FROM families
            """
        ).fetchone()
    return {
        "active_families": int(row["active_families"] or 0),
        "active_applications": int(row["active_applications"] or 0),
        "active_companies": int(row["active_companies"] or 0),
        "validated_sources": int(row["validated_sources"] or 0),
    }


def _inventory_ready(database: LiveDatabase) -> bool:
    """Reject tiny partial recoveries instead of permanently treating them as ready."""
    counts = _inventory_counts(database)
    minimum_applications = max(
        1, int(os.getenv("GAIA_BOOTSTRAP_MIN_ACTIVE_APPLICATIONS", "100"))
    )
    minimum_companies = max(
        1, int(os.getenv("GAIA_BOOTSTRAP_MIN_ACTIVE_COMPANIES", "20"))
    )
    minimum_sources = max(
        1, int(os.getenv("GAIA_BOOTSTRAP_MIN_VALIDATED_SOURCES", "25"))
    )
    return (
        counts["active_applications"] >= minimum_applications
        and counts["active_companies"] >= minimum_companies
        and counts["validated_sources"] >= minimum_sources
    )


def _claim_empty_database_bootstrap(database: LiveDatabase, worker_id: str) -> str:
    """Return ready, claimed, or busy while leasing across serverless instances."""
    if _inventory_ready(database):
        return "ready"
    lease_seconds = max(60, int(os.getenv("GAIA_BOOTSTRAP_LEASE_SECONDS", "180")))
    with database.connect() as connection:
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
    return "claimed" if row is not None else "busy"


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


async def bootstrap_empty_database() -> bool:
    """Initialize and recover an empty or materially incomplete project."""
    if os.getenv("GAIA_BOOTSTRAP_EMPTY_DATABASE", "1") != "1":
        return True

    database = LiveDatabase(migrate=False)
    try:
        await asyncio.to_thread(_initialize_schema, database)
    except Exception:
        LOGGER.exception("could not initialize the recreated Supabase schema")
        return False

    worker_id = _worker_id()
    try:
        claim_state = _claim_empty_database_bootstrap(database, worker_id)
    except Exception:
        LOGGER.exception("could not inspect the recreated database")
        return False
    if claim_state == "ready":
        return True
    if claim_state == "busy":
        return False

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

        if status == "ok":
            try:
                if not _inventory_ready(database):
                    counts = _inventory_counts(database)
                    status = "partial"
                    error = f"inventory recovery remains below readiness floor: {counts}"
                    LOGGER.warning(error)
            except Exception as exc:
                status = "broken"
                error = error or repr(exc)
                LOGGER.exception("could not evaluate recovered inventory readiness")

        try:
            _finish_empty_database_bootstrap(
                database,
                worker_id,
                status=status,
                error=error,
            )
        except Exception:
            LOGGER.exception("could not release empty database bootstrap lease")

    try:
        return _inventory_ready(database)
    except Exception:
        LOGGER.exception("could not verify recovered inventory")
        return False
