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

_DATABASE_VARIABLES = (
    "GAIA_DATABASE_URL",
    "POSTGRES_URL",
    "DATABASE_URL",
    "SUPABASE_DB_URL",
)
_DATABASE_NOT_CONFIGURED = (
    "PostgreSQL is not configured. Set GAIA_DATABASE_URL (or DATABASE_URL) "
    "to the Supabase pooler connection string."
)
_SCHEMA_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_database_url(value: str) -> str:
    """Remove integration metadata that libpq does not recognize."""
    parts = urlsplit(value)
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "supa"
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


# Vercel's Supabase integration provides POSTGRES_URL automatically. Normalize it
# into GAIA's explicit name before importing the database implementation so local,
# Vercel, and standalone Supabase environments share one code path.
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
        # Families are the public read model. Exclude roles already classified as
        # non-internships or the wrong cycle, but preserve unknown-cycle internships:
        # those are real opportunities and are intentionally visible in the broad feed.
        translated = translated.replace(
            "WHERE active AND target_match!='not_internship'",
            "WHERE active AND target_match IN "
            "('exact','year_confirmed','source_confirmed','unknown')",
        )
        # Psycopg treats every percent sign in a parameterized query as part of
        # its placeholder grammar. Preserve supported placeholders and already
        # escaped percents, while escaping SQL literals such as ILIKE '%remote%'.
        return re.sub(r"(?<!%)%(?![sbt%])", "%%", translated)

    def executemany(self, query: str, params_seq: Any) -> None:
        with self._connection.cursor() as cursor:
            cursor.executemany(self._query(query), params_seq)


class Database(WriteMixin, ReadMixin, BaseDatabase):
    """GAIA's PostgreSQL repository and query service.

    Constructing the repository is intentionally safe without database credentials.
    Web frameworks and build systems import application modules before runtime secrets
    are necessarily available. Configuration is therefore required only when a real
    database operation begins, not while importing the ASGI application.
    """

    def __init__(
        self,
        url: str | Path | None = None,
        *,
        schema: str | None = None,
        migrate: bool | None = None,
    ) -> None:
        # The pre-PostgreSQL test suite passes a unique temporary path to request
        # an isolated database. BaseDatabase maps that path to a deterministic
        # PostgreSQL schema; force its idempotent migration even when CI disables
        # automatic production migrations globally.
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
        # BaseDatabase owns transaction commit/rollback and connection cleanup.
        # Replace only the compatibility adapter so bulk writes use a cursor,
        # which is where psycopg3 implements executemany().
        with BaseDatabase.connect(self) as adapter:
            yield _PsycopgConnectionAdapter(adapter._connection)

    def migrate(self) -> None:
        self._require_configuration()
        BaseDatabase.migrate(self)


__all__ = [
    "Database",
    "EMPLOYER_DATE_MODES",
    "TARGET_MATCHES",
    "application_identity",
    "coverage_role_signature",
    "iso",
]
