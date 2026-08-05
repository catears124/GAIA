from __future__ import annotations

import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import errors, sql

APPLICATION_NAME = "gaia-index-repair-v4"
SUPERSEDED_APPLICATION_NAMES = (
    "gaia-index-repair",
    "gaia-index-repair-v2",
    "gaia-index-repair-v3",
)
LOCK_NAME = "gaia-postings-index-repair-v4"
LOCK_WAIT_SECONDS = 60
INDEXES = {
    "idx_postings_source_inventory": """
        CREATE INDEX CONCURRENTLY idx_postings_source_inventory
        ON postings (source, target_match)
        INCLUDE (posting_key, active, canonical_apply_url, source_id)
    """,
    "idx_postings_active_lead_reconcile": """
        CREATE INDEX CONCURRENTLY idx_postings_active_lead_reconcile
        ON postings (source_mode)
        WHERE active
          AND source_mode IN ('registry', 'external-index', 'verification-lead')
    """,
}


def _database_url() -> str:
    url = os.getenv("GAIA_MIGRATION_DATABASE_URL") or os.getenv("GAIA_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "PostgreSQL is not configured. Set GAIA_MIGRATION_DATABASE_URL or "
            "GAIA_DATABASE_URL."
        )
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"supa", "application_name"}
    ]
    if "supabase.com" in parts.netloc and not any(
        key.lower() == "sslmode" for key, _ in query
    ):
        query.append(("sslmode", "require"))
    query.append(("application_name", APPLICATION_NAME))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _index_state(connection: psycopg.Connection, name: str) -> tuple[bool, bool] | None:
    row = connection.execute(
        """
        SELECT index_state.indisvalid, index_state.indisready
        FROM pg_class AS index_class
        JOIN pg_index AS index_state ON index_state.indexrelid=index_class.oid
        JOIN pg_namespace AS namespace ON namespace.oid=index_class.relnamespace
        WHERE namespace.nspname=current_schema()
          AND index_class.relname=%s
        """,
        (name,),
    ).fetchone()
    if row is None:
        return None
    return bool(row[0]), bool(row[1])


def _terminate_pids(
    connection: psycopg.Connection,
    rows: list[tuple[int, str]],
    *,
    reason: str,
) -> None:
    for pid, application_name in rows:
        terminated = connection.execute(
            "SELECT pg_terminate_backend(%s)", (int(pid),)
        ).fetchone()
        print(
            f"terminated {reason} pid={pid} app={application_name!r}: "
            f"{bool(terminated and terminated[0])}"
        )


def _terminate_superseded_repairs(connection: psycopg.Connection) -> None:
    rows = connection.execute(
        """
        SELECT pid, application_name
        FROM pg_stat_activity
        WHERE datname=current_database()
          AND pid<>pg_backend_pid()
          AND application_name = ANY(%s)
        """,
        (list(SUPERSEDED_APPLICATION_NAMES),),
    ).fetchall()
    _terminate_pids(connection, rows, reason="superseded repair")


def _acquire_repair_lock(connection: psycopg.Connection) -> None:
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        row = connection.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_NAME,)
        ).fetchone()
        if row and bool(row[0]):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for the production index repair lock")
        time.sleep(2)


def _drop_invalid_index(connection: psycopg.Connection, name: str) -> None:
    state = _index_state(connection, name)
    if state is None or state[0]:
        return
    print(f"dropping invalid index concurrently: {name} state={state}")
    connection.execute(
        sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(name))
    )


def _build_index(connection: psycopg.Connection, name: str, statement: str) -> None:
    for attempt in range(1, 4):
        _drop_invalid_index(connection, name)
        if _index_state(connection, name) == (True, True):
            print(f"index ready: {name}")
            return
        try:
            print(f"building index concurrently: {name} attempt={attempt}")
            connection.execute(statement)
            return
        except errors.DuplicateTable:
            state = _index_state(connection, name)
            if state == (True, True):
                return
            if attempt == 3:
                raise
        except (errors.LockNotAvailable, errors.QueryCanceled):
            if attempt == 3:
                raise
            print(f"contention building {name}; retrying without blocking writers")
            time.sleep(5)


def repair_indexes() -> None:
    schema = os.getenv("GAIA_SCHEMA", "public")
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        connection.execute("SET statement_timeout = '12min'")
        connection.execute("SET lock_timeout = '10s'")
        _terminate_superseded_repairs(connection)
        _acquire_repair_lock(connection)
        try:
            for name, statement in INDEXES.items():
                if _index_state(connection, name) == (True, True):
                    print(f"index ready: {name}")
                    continue
                _build_index(connection, name, statement)
                if _index_state(connection, name) != (True, True):
                    raise RuntimeError(f"index build did not become ready and valid: {name}")
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))


if __name__ == "__main__":
    repair_indexes()
