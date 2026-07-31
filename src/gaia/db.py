from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
from dotenv import load_dotenv
from psycopg import sql

load_dotenv()

_DATABASE_VARIABLES = (
    "GAIA_DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "DATABASE_URL",
    "SUPABASE_DB_URL",
    "POSTGRES_URL_NON_POOLING",
)
_MIGRATION_DATABASE_VARIABLES = (
    "GAIA_MIGRATION_DATABASE_URL",
    "GAIA_ADMIN_DATABASE_URL",
    "POSTGRES_URL_NON_POOLING",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "DATABASE_URL",
    "SUPABASE_DB_URL",
)
_DATABASE_NOT_CONFIGURED = (
    "PostgreSQL is not configured. Set POSTGRES_URL, POSTGRES_URL_NON_POOLING, "
    "POSTGRES_PRISMA_URL, or GAIA_DATABASE_URL."
)
_SCHEMA_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_LIBPQ_QUERY_PARAMETERS = {
    "connection_limit",
    "pgbouncer",
    "pool_timeout",
    "supa",
}
_MIGRATION_LOCK = threading.Lock()
_MIGRATED: set[tuple[str, str, str]] = set()


def _normalize_database_url(value: str) -> str:
    """Normalize Vercel/Supabase URLs into parameters understood by libpq."""
    value = value.strip()
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    parts = urlsplit(value)
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _NON_LIBPQ_QUERY_PARAMETERS
    ]
    if "supabase" in parts.netloc.casefold() and not any(
        key.casefold() == "sslmode" for key, _ in query
    ):
        query.append(("sslmode", "require"))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _component_database_url() -> str | None:
    """Build a connection URL from the standard Supabase component variables."""
    host = os.getenv("POSTGRES_HOST", "").strip()
    user = os.getenv("POSTGRES_USER", "").strip()
    password = os.getenv("POSTGRES_PASSWORD", "")
    database = os.getenv("POSTGRES_DATABASE", "").strip()
    if not all((host, user, password, database)):
        return None
    if ":" in host and not host.startswith("[") and host.count(":") > 1:
        host = f"[{host}]"
    return (
        "postgresql://"
        f"{quote(user, safe='')}:{quote(password, safe='')}@{host}/"
        f"{quote(database, safe='')}?sslmode=require"
    )


def _configured_database_url(*, migration: bool = False) -> str | None:
    variables = _MIGRATION_DATABASE_VARIABLES if migration else _DATABASE_VARIABLES
    for variable in variables:
        if value := os.getenv(variable):
            return _normalize_database_url(value)
    return _component_database_url()


def _is_legacy_path(value: str | Path | None) -> bool:
    if isinstance(value, Path):
        return True
    if not isinstance(value, str):
        return False
    candidate = value.strip().casefold()
    return (
        candidate == ":memory:"
        or candidate.endswith((".db", ".sqlite", ".sqlite3"))
        or "/" in candidate
        or "\\" in candidate
    ) and "://" not in candidate


# Normalize the standard Vercel Supabase integration variables before importing the
# database implementation so every process follows one connection path.
if database_url := _configured_database_url():
    os.environ["GAIA_DATABASE_URL"] = database_url

from .db_base import (  # noqa: E402
    EMPLOYER_DATE_MODES,
    TARGET_MATCHES,
    BaseDatabase,
    ConnectionAdapter,
    application_identity,
    coverage_role_signature,
    iso,
)
from .db_read import ReadMixin  # noqa: E402
from .db_write import WriteMixin  # noqa: E402


class _PsycopgConnectionAdapter(ConnectionAdapter):
    """Psycopg3 adapter for legacy SQL and cursor-backed bulk execution."""

    @staticmethod
    def _query(query: str) -> str:
        translated = ConnectionAdapter._query(query)
        translated = translated.replace(
            "WHERE active AND target_match!='not_internship'",
            "WHERE active AND target_match IN "
            "('exact','year_confirmed','source_confirmed','unknown')",
        )
        return re.sub(r"(?<!%)%(?![sbt%])", "%%", translated)

    def executemany(self, query: str, params_seq: Any) -> None:
        with self._connection.cursor() as cursor:
            cursor.executemany(self._query(query), params_seq)


class Database(WriteMixin, ReadMixin, BaseDatabase):
    """GAIA's PostgreSQL repository and query service."""

    def __init__(
        self,
        url: str | Path | None = None,
        *,
        schema: str | None = None,
        migrate: bool | None = None,
    ) -> None:
        if migrate is None and _is_legacy_path(url):
            migrate = True

        if url is None and _configured_database_url() is None:
            self.url: str | None = None
            self.path = self
            self.schema = schema or os.getenv("GAIA_SCHEMA", "public")
            if not _SCHEMA_PATTERN.fullmatch(self.schema):
                raise ValueError(f"invalid PostgreSQL schema name: {self.schema!r}")
            self.timeout = max(1, int(float(os.getenv("GAIA_DB_TIMEOUT", "60"))))
            return

        super().__init__(url, schema=schema, migrate=migrate)

    def _require_configuration(self) -> None:
        if self.url is None:
            raise RuntimeError(_DATABASE_NOT_CONFIGURED)

    @contextmanager
    def connect(self) -> Iterator[_PsycopgConnectionAdapter]:
        self._require_configuration()
        with BaseDatabase.connect(self) as adapter:
            yield _PsycopgConnectionAdapter(adapter._connection)

    def migrate(self) -> None:
        """Idempotently initialize a new Supabase project through its direct URL."""
        self._require_configuration()
        migration_url = _configured_database_url(migration=True) or self.url
        assert migration_url is not None

        schema_sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        from .employer_census import ECOSYSTEM_SCHEMA_STATEMENTS
        from .universe import UNIVERSE_SCHEMA_STATEMENTS

        statements = (*UNIVERSE_SCHEMA_STATEMENTS, *ECOSYSTEM_SCHEMA_STATEMENTS)
        fingerprint = hashlib.sha256(
            (schema_sql + "\n" + "\n".join(statements)).encode()
        ).hexdigest()
        identity = (migration_url, self.schema, fingerprint)

        with _MIGRATION_LOCK:
            if identity in _MIGRATED:
                return
            with psycopg.connect(
                migration_url,
                connect_timeout=min(self.timeout, 12),
                application_name="gaia-runtime-bootstrap",
                prepare_threshold=None,
                options="-c statement_timeout=0 -c lock_timeout=0",
            ) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"gaia:schema:{self.schema}",),
                )
                connection.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(self.schema)
                    )
                )
                connection.execute(
                    sql.SQL("SET search_path TO {}, public").format(
                        sql.Identifier(self.schema)
                    )
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gaia_schema_state (
                        name TEXT PRIMARY KEY,
                        fingerprint TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                row = connection.execute(
                    "SELECT fingerprint FROM gaia_schema_state WHERE name='core'"
                ).fetchone()
                if row is None or str(row[0]) != fingerprint:
                    connection.execute(schema_sql)
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        """
                        INSERT INTO gaia_schema_state(name, fingerprint, applied_at)
                        VALUES ('core', %s, now())
                        ON CONFLICT(name) DO UPDATE SET
                            fingerprint=excluded.fingerprint,
                            applied_at=excluded.applied_at
                        """,
                        (fingerprint,),
                    )

                # A recreated database needs at least one native source and immediately
                # due discovery work. The runtime bootstrap will recover registry leads
                # and validate provider boards without requiring any extra secret.
                connection.execute(
                    """
                    INSERT INTO source_catalog(
                        source, kind, scope, spec, validated, origin
                    ) VALUES (
                        'google-careers', 'google-careers', 'current',
                        '{}'::jsonb, TRUE, 'runtime-bootstrap'
                    )
                    ON CONFLICT(source) DO UPDATE SET
                        kind=excluded.kind,
                        scope='current',
                        spec=excluded.spec,
                        validated=TRUE,
                        origin=CASE
                            WHEN source_catalog.origin='legacy' THEN excluded.origin
                            ELSE source_catalog.origin
                        END,
                        last_discovered_at=now()
                    """
                )
                connection.execute(
                    """
                    INSERT INTO crawl_targets(
                        source, enabled, scheduled, priority, interval_seconds, next_run_at
                    ) VALUES ('google-careers', TRUE, TRUE, 30, 1200, now())
                    ON CONFLICT(source) DO UPDATE SET
                        enabled=TRUE,
                        scheduled=TRUE,
                        next_run_at=LEAST(crawl_targets.next_run_at, now()),
                        updated_at=now()
                    """
                )
                connection.execute(
                    """
                    INSERT INTO worker_tasks(task_key, next_run_at)
                    VALUES
                        ('market-discovery', now()),
                        ('universe-discovery', now() + interval '1 hour'),
                        ('empty-database-bootstrap', now())
                    ON CONFLICT(task_key) DO NOTHING
                    """
                )
            _MIGRATED.add(identity)


__all__ = [
    "Database",
    "EMPLOYER_DATE_MODES",
    "TARGET_MATCHES",
    "application_identity",
    "coverage_role_signature",
    "iso",
]
