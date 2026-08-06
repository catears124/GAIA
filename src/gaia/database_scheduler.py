from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit

from .db import Database

_JOB_NAME = "gaia-continuous-runtime-pulse"

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


def _record(database: Database, *, ready: bool, detail: str) -> None:
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
            (_JOB_NAME, ready, ready, detail[:2000]),
        )


def install_database_scheduler(database: Database | None = None) -> dict[str, object]:
    """Install a Supabase-native minute pulse independent of GitHub Actions.

    Supabase exposes pg_cron and pg_net. The cron job only performs a bounded HTTP
    wake-up; PostgreSQL never executes crawler code or holds a long transaction.
    """
    database = database or Database(migrate=False)
    base_url = _base_url()
    try:
        with database.connect() as connection:
            connection.execute(_STATE_SCHEMA)
            connection.execute("CREATE EXTENSION IF NOT EXISTS pg_cron")
            connection.execute(
                "CREATE EXTENSION IF NOT EXISTS pg_net WITH SCHEMA extensions"
            )
            row = connection.execute(
                """
                SELECT cron.schedule(
                    %s,
                    '* * * * *',
                    format(
                        $command$
                        SELECT net.http_post(
                            url := %L,
                            headers := jsonb_build_object(
                                'Content-Type', 'application/json',
                                'User-Agent', 'GAIA-production-maintenance/supabase-cron'
                            ),
                            body := jsonb_build_object(
                                'scheduler', 'supabase-pg-cron',
                                'requested_at', now()
                            ),
                            timeout_milliseconds := 45000
                        ) AS request_id
                        $command$,
                        %s
                    )
                ) AS job_id
                """,
                (_JOB_NAME, f"{base_url}/api/maintenance/continuous-tick"),
            ).fetchone()
            job_id = int(dict(row or {}).get("job_id") or 0)
            verification = connection.execute(
                """
                SELECT jobid, active, schedule
                FROM cron.job
                WHERE jobname=%s
                """,
                (_JOB_NAME,),
            ).fetchone()
        payload = dict(verification or {})
        ready = bool(payload.get("active")) and payload.get("schedule") == "* * * * *"
        detail = f"job_id={job_id}; schedule={payload.get('schedule')}; active={payload.get('active')}"
        _record(database, ready=ready, detail=detail)
        return {"ready": ready, "job_id": job_id, "detail": detail}
    except Exception as error:
        try:
            _record(database, ready=False, detail=repr(error))
        except Exception:
            pass
        return {"ready": False, "job_id": None, "detail": repr(error)}


def scheduler_status(database: Database | None = None) -> dict[str, object]:
    database = database or Database(migrate=False)
    try:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT scheduler_key, installed_at, last_checked_at, ready, detail
                FROM gaia_scheduler_state
                WHERE scheduler_key=%s
                """,
                (_JOB_NAME,),
            ).fetchone()
    except Exception as error:
        return {"ready": False, "detail": repr(error)}
    return dict(row or {"ready": False, "detail": "not installed"})


def main() -> int:
    result = install_database_scheduler()
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
