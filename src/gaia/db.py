from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()

# An explicit GAIA override is authoritative. Vercel's Supabase integration aliases
# follow, preferring the transaction pooler used by Prisma over the direct session URL.
# Recreated Supabase projects can leave one alias stale, so callers that expose several
# credentials should still probe them rather than assuming the first non-empty value works.
_DATABASE_VARIABLES = (
    "GAIA_DATABASE_URL",
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL",
    "POSTGRES_URL_NON_POOLING",
    "DATABASE_URL",
    "SUPABASE_DB_URL",
)
_DATABASE_NOT_CONFIGURED = (
    "PostgreSQL is not configured. Set GAIA_DATABASE_URL or POSTGRES_PRISMA_URL "
    "to the active Supabase connection string."
)
_SCHEMA_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Prisma and hosting integrations append client-only options such as `pgbouncer`,
# `connection_limit`, `pool_timeout`, and `supa`. libpq rejects unknown URI options
# with psycopg.ProgrammingError before it can contact PostgreSQL. Keep only options
# accepted by libpq; GAIA configures statement timeouts after connecting.
_LIBPQ_QUERY_PARAMETERS = frozenset(
    {
        "application_name",
        "channel_binding",
        "client_encoding",
        "connect_timeout",
        "fallback_application_name",
        "gssencmode",
        "hostaddr",
        "keepalives",
        "keepalives_count",
        "keepalives_idle",
        "keepalives_interval",
        "krbsrvname",
        "load_balance_hosts",
        "options",
        "passfile",
        "replication",
        "requirepeer",
        "service",
        "servicefile",
        "sslcert",
        "sslcompression",
        "sslcrl",
        "sslcrldir",
        "sslkey",
        "sslmode",
        "sslpassword",
        "sslrootcert",
        "ssl_max_protocol_version",
        "ssl_min_protocol_version",
        "target_session_attrs",
        "tcp_user_timeout",
    }
)


def _normalize_database_url(value: str) -> str:
    """Strip hosting/client metadata that psycopg/libpq cannot parse."""
    parts = urlsplit(value.strip())
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() in _LIBPQ_QUERY_PARAMETERS
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _configured_database_url() -> str | None:
    for variable in _DATABASE_VARIABLES:
        if value := os.getenv(variable):
            return _normalize_database_url(value)
    return None


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
from .removal_guard import GuardedWriteMixin  # noqa: E402


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


class Database(GuardedWriteMixin, ReadMixin, BaseDatabase):
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
            self.timeout = max(
                1,
                int(
                    float(
                        os.getenv(
                            "GAIA_DB_TIMEOUT",
                            "8" if os.getenv("VERCEL") else "60",
                        )
                    )
                ),
            )
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
        self._require_configuration()
        BaseDatabase.migrate(self)

    def rebuild_families(self) -> int:
        """Publish families once at a time with an automatically released lock.

        A full rebuild replaces the family read model inside one transaction. Concurrent
        rebuilds caused unique-key violations, deadlocks, and verified roles disappearing.
        The transaction-scoped PostgreSQL advisory lock is safe with Supabase's transaction
        pooler: it is released automatically on commit, rollback, timeout, or disconnect.
        A busy caller returns immediately and the bounded maintenance loop retries.
        """
        lock_name = f"gaia:family-rebuild-v2:{self.schema}"
        with self.connect() as lock_connection:
            row = lock_connection.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS acquired",
                (lock_name,),
            ).fetchone()
            if not bool(dict(row or {}).get("acquired")):
                return 0

            super().rebuild_families()
            with self.connect() as connection:
                connection.execute(
                    """
                    WITH normalized AS (
                        SELECT
                            family_key,
                            COALESCE(
                                (
                                    SELECT jsonb_agg(
                                        CASE
                                            WHEN opening->>'source_mode'='verification' THEN
                                                jsonb_set(
                                                    jsonb_set(
                                                        opening,
                                                        '{source_mode}',
                                                        '"direct"'::jsonb,
                                                        TRUE
                                                    ),
                                                    '{verification_mode}',
                                                    '"employer-page"'::jsonb,
                                                    TRUE
                                                )
                                            ELSE opening
                                        END
                                        ORDER BY ordinality
                                    )
                                    FROM jsonb_array_elements(openings)
                                        WITH ORDINALITY AS item(opening, ordinality)
                                ),
                                '[]'::jsonb
                            ) AS normalized_openings,
                            (
                                SELECT COUNT(*)
                                FROM jsonb_array_elements(openings) AS opening
                                WHERE opening->>'source_mode' IN ('direct','verification')
                            )::integer AS verified_openings
                        FROM families
                    )
                    UPDATE families AS family
                    SET openings=normalized.normalized_openings,
                        direct_openings=normalized.verified_openings,
                        backstop_openings=family.opening_count-normalized.verified_openings
                    FROM normalized
                    WHERE family.family_key=normalized.family_key
                    """
                )
            return 1


__all__ = [
    "Database",
    "EMPLOYER_DATE_MODES",
    "TARGET_MATCHES",
    "application_identity",
    "coverage_role_signature",
    "iso",
]
