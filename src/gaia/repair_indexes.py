from __future__ import annotations

import os

import psycopg
from psycopg import sql


LOCK_NAME = "gaia-postings-index-repair-v1"
INDEXES = {
    "idx_postings_source_inventory": """
        CREATE INDEX CONCURRENTLY idx_postings_source_inventory
        ON postings (source, target_match)
        INCLUDE (posting_key, active, canonical_apply_url, source_id)
    """,
    "idx_postings_active_lead_reconcile": """
        CREATE INDEX CONCURRENTLY idx_postings_active_lead_reconcile
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
    if "supabase.com" in url and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


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


def repair_indexes() -> None:
    schema = os.getenv("GAIA_SCHEMA", "public")
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        connection.execute("SET statement_timeout = 0")
        connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_NAME,))
        try:
            for name, statement in INDEXES.items():
                if _index_is_valid(connection, name):
                    print(f"index ready: {name}")
                    continue
                connection.execute(
                    sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(
                        sql.Identifier(name)
                    )
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
