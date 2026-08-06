from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

from .db import Database

_PRIMARY_JOB_NAME = "gaia-continuous-runtime-pulse"


@dataclass(frozen=True, slots=True)
class SchedulerJob:
    name: str
    schedule: str
    path: str
    timeout_milliseconds: int


_JOBS = (
    SchedulerJob(
        name=_PRIMARY_JOB_NAME,
        schedule="* * * * *",
        path="/api/maintenance/continuous-tick",
        timeout_milliseconds=45_000,
    ),
    SchedulerJob(
        name="gaia-runtime-lead-verification",
        schedule="*/2 * * * *",
        path="/api/maintenance/verify-fresh-leads",
        timeout_milliseconds=25_000,
    ),
    SchedulerJob(
        name="gaia-runtime-market-discovery",
        schedule="3,18,33,48 * * * *",
        path="/api/maintenance/discover",
        timeout_milliseconds=25_000,
    ),
)

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS gaia_scheduler_state (
    scheduler_key TEXT PRIMARY KEY,
    installed_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready BOOLEAN NOT NULL DEFAULT FALSE,
    detail TEXT
);
ALTER TABLE gaia_scheduler_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE gaia_scheduler_state FROM anon, authenticated;
"""


def _base_url() -> str:
    value = os.getenv("GAIA_PUBLIC_BASE_URL", "https://gaiajob.vercel.app").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "gaiajob.vercel.app" or parsed.path:
        raise RuntimeError("GAIA_PUBLIC_BASE_URL must be https://gaiajob.vercel.app")
    return value


def _record(
    database: Database,
    *,
    scheduler_key: str,
    ready: bool,
    detail: str,
) -> None:
    with database.connect() as connection:
        connection.execute(_STATE_SCHEMA)
        connection.execute(
            """
            INSERT INTO gaia_scheduler_state(
                scheduler_key, installed_at, last_checked_at, ready, detail
            )
            VALUES (
                %s,
                CASE WHEN %s THEN now() ELSE NULL END,
                now(),
                %s,
                %s
            )
            ON CONFLICT(scheduler_key) DO UPDATE
            SET installed_at=CASE
                    WHEN EXCLUDED.ready THEN COALESCE(
                        gaia_scheduler_state.installed_at,
                        EXCLUDED.installed_at
                    )
                    ELSE gaia_scheduler_state.installed_at
                END,
                last_checked_at=now(),
                ready=EXCLUDED.ready,
                detail=EXCLUDED.detail
            """,
            (scheduler_key, ready, ready, detail[:2000]),
        )


def _extension_installed(database: Database, name: str) -> bool:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname=%s) AS installed",
            (name,),
        ).fetchone()
    return bool(dict(row or {}).get("installed"))


def _ensure_extensions(database: Database) -> None:
    # Avoid issuing CREATE EXTENSION against an already-provisioned Supabase project;
    # some pooler roles can use installed extensions but cannot create them.
    if not _extension_installed(database, "pg_cron"):
        with database.connect() as connection:
            connection.execute("CREATE EXTENSION pg_cron")
    if not _extension_installed(database, "pg_net"):
        with database.connect() as connection:
            connection.execute("CREATE EXTENSION pg_net WITH SCHEMA extensions")


def _cron_command(base_url: str, job: SchedulerJob) -> str:
    # base_url and every path are fixed/allowlisted, so interpolating the endpoint into
    # pg_cron's stored command cannot introduce arbitrary SQL. The complete command is
    # then passed as one ordinary psycopg parameter to cron.schedule.
    endpoint = f"{base_url}{job.path}"
    return f"""
        SELECT net.http_post(
            url := '{endpoint}',
            headers := jsonb_build_object(
                'Content-Type', 'application/json',
                'User-Agent', 'GAIA-production-maintenance/supabase-cron'
            ),
            body := jsonb_build_object(
                'scheduler', 'supabase-pg-cron',
                'job', '{job.name}',
                'requested_at', now()
            ),
            timeout_milliseconds := {job.timeout_milliseconds}
        ) AS request_id
    """


def _install_job(
    database: Database,
    *,
    base_url: str,
    job: SchedulerJob,
) -> dict[str, object]:
    try:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT cron.schedule(%s, %s, %s) AS job_id",
                (job.name, job.schedule, _cron_command(base_url, job)),
            ).fetchone()
            job_id = int(dict(row or {}).get("job_id") or 0)
            verification = connection.execute(
                """
                SELECT jobid, active, schedule
                FROM cron.job
                WHERE jobname=%s
                """,
                (job.name,),
            ).fetchone()
        payload = dict(verification or {})
        ready = bool(payload.get("active")) and payload.get("schedule") == job.schedule
        detail = (
            f"job_id={job_id}; schedule={payload.get('schedule')}; "
            f"active={payload.get('active')}; path={job.path}"
        )
        _record(
            database,
            scheduler_key=job.name,
            ready=ready,
            detail=detail,
        )
        return {
            "scheduler_key": job.name,
            "ready": ready,
            "job_id": job_id,
            "schedule": job.schedule,
            "path": job.path,
            "detail": detail,
        }
    except Exception as error:
        try:
            _record(
                database,
                scheduler_key=job.name,
                ready=False,
                detail=repr(error),
            )
        except Exception:
            pass
        return {
            "scheduler_key": job.name,
            "ready": False,
            "job_id": None,
            "schedule": job.schedule,
            "path": job.path,
            "detail": repr(error),
        }


def install_database_scheduler(database: Database | None = None) -> dict[str, object]:
    """Install Supabase-native inventory, verification, and discovery clocks.

    PostgreSQL only emits bounded HTTP wake-ups. Crawling and verification stay inside
    independently leased Vercel requests, so one slow subsystem cannot block the others.
    """
    database = database or Database(migrate=False)
    base_url = _base_url()
    try:
        with database.connect() as connection:
            connection.execute(_STATE_SCHEMA)
        _ensure_extensions(database)
    except Exception as error:
        for job in _JOBS:
            try:
                _record(
                    database,
                    scheduler_key=job.name,
                    ready=False,
                    detail=repr(error),
                )
            except Exception:
                pass
        return {"ready": False, "jobs": [], "detail": repr(error)}

    jobs = [
        _install_job(database, base_url=base_url, job=job)
        for job in _JOBS
    ]
    primary = next(item for item in jobs if item["scheduler_key"] == _PRIMARY_JOB_NAME)
    ready = all(item.get("ready") is True for item in jobs)
    return {
        "ready": ready,
        "job_id": primary.get("job_id"),
        "detail": "; ".join(str(item.get("detail")) for item in jobs),
        "jobs": jobs,
    }


def scheduler_status(
    database: Database | None = None,
    *,
    scheduler_key: str = _PRIMARY_JOB_NAME,
) -> dict[str, object]:
    database = database or Database(migrate=False)
    try:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT scheduler_key, installed_at, last_checked_at, ready, detail
                FROM gaia_scheduler_state
                WHERE scheduler_key=%s
                """,
                (scheduler_key,),
            ).fetchone()
    except Exception as error:
        return {"scheduler_key": scheduler_key, "ready": False, "detail": repr(error)}
    return dict(
        row
        or {
            "scheduler_key": scheduler_key,
            "ready": False,
            "detail": "not installed",
        }
    )


def all_scheduler_statuses(database: Database | None = None) -> list[dict[str, object]]:
    database = database or Database(migrate=False)
    try:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT scheduler_key, installed_at, last_checked_at, ready, detail
                FROM gaia_scheduler_state
                WHERE scheduler_key = ANY(%s)
                ORDER BY scheduler_key
                """,
                ([job.name for job in _JOBS],),
            ).fetchall()
    except Exception as error:
        return [
            {
                "scheduler_key": job.name,
                "ready": False,
                "detail": repr(error),
            }
            for job in _JOBS
        ]
    by_key = {str(row["scheduler_key"]): dict(row) for row in rows}
    return [
        by_key.get(
            job.name,
            {
                "scheduler_key": job.name,
                "ready": False,
                "detail": "not installed",
            },
        )
        for job in _JOBS
    ]


def main() -> int:
    result = install_database_scheduler()
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
