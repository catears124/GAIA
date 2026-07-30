from __future__ import annotations

import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql


LOCK_NAME = "gaia-postings-index-repair-v1"
LOCK_WAIT_SECONDS = 15 * 60
INDEX_NAMES = (
    "idx_postings_source_inventory",
    "idx_postings_active_lead_reconcile",
)
INDEXES = {
    "idx_postings_source_inventory": """
        CREATE INDEX idx_postings_source_inventory
        ON postings (source, target_match)
        INCLUDE (posting_key, active, canonical_apply_url, source_id)
    """,
    "idx_postings_active_lead_reconcile": """
        CREATE INDEX idx_postings_active_lead_reconcile
        ON postings (target_match, source_mode)
        INCLUDE (posting_key, company, canonical_apply_url, source, source_id)
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
        if key.lower() != "supa"
    ]
    if "supabase.com" in parts.netloc and not any(
        key.lower() == "sslmode" for key, _ in query
    ):
        query.append(("sslmode", "require"))
    query.append(("application_name", "gaia-index-repair"))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _index_is_valid(connection: psycopg.Connection, name: str) -> bool:
    row = connection.execute(
        """
        SELECT index_state.indisvalid
        FROM pg_class AS index_class
        JOIN pg_index AS index_state ON index_state.indexrelid=index_class.oid
        JOIN pg_namespace AS namespace ON namespace.oid=index_class.relnamespace
        WHERE namespace.nspname=current_schema()
          AND index_class.relname=%s
        """,
        (name,),
    ).fetchone()
    return bool(row and row[0])


def _cancel_stale_index_builds(connection: psycopg.Connection) -> None:
    rows = connection.execute(
        """
        SELECT pid
        FROM pg_stat_activity
        WHERE datname=current_database()
          AND pid<>pg_backend_pid()
          AND query_start < now() - interval '30 seconds'
          AND query ~* 'CREATE[[:space:]]+INDEX([[:space:]]+CONCURRENTLY)?[[:space:]]+(idx_postings_source_inventory|idx_postings_active_lead_reconcile)'
        """
    ).fetchall()
    for row in rows:
        pid = int(row[0])
        cancelled = connection.execute(
            "SELECT pg_cancel_backend(%s)", (pid,)
        ).fetchone()
        print(f"cancelled stale index builder pid={pid}: {bool(cancelled and cancelled[0])}")


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
        _cancel_stale_index_builds(connection)
        time.sleep(2)


def repair_indexes() -> None:
    schema = os.getenv("GAIA_SCHEMA", "public")
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        connection.execute("SET statement_timeout = 0")
        connection.execute("SET lock_timeout = '15min'")
        _cancel_stale_index_builds(connection)
        _acquire_repair_lock(connection)
        try:
            for name, statement in INDEXES.items():
                if _index_is_valid(connection, name):
                    print(f"index ready: {name}")
                    continue
                connection.execute(
                    sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(name))
                )
                print(f"building index: {name}")
                connection.execute(statement)
                if not _index_is_valid(connection, name):
                    raise RuntimeError(f"index build did not become valid: {name}")
            connection.execute("ANALYZE postings")
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))


if __name__ == "__main__":
    repair_indexes()
