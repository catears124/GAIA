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


def _is_legacy_path(value: str | Path | None) -> bool:
    return isinstance(value, Path) or (
        isinstance(value, str) and "://" not in value and not value.startswith("postgres")
    )


# Vercel's Supabase integration provides POSTGRES_URL automatically. Normalize it
# into GAIA's explicit name before importing the database implementation so local,
# Vercel, and standalone Supabase environments share one code path.
database_url = os.getenv("GAIA_DATABASE_URL")
if not database_url:
    for variable in ("POSTGRES_URL", "DATABASE_URL", "SUPABASE_DB_URL"):
        if value := os.getenv(variable):
            database_url = value
            break
if database_url:
    os.environ["GAIA_DATABASE_URL"] = _normalize_database_url(database_url)

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
        # Psycopg treats every percent sign in a parameterized query as part of
        # its placeholder grammar. Preserve supported placeholders and already
        # escaped percents, while escaping SQL literals such as ILIKE '%remote%'.
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
        # The pre-PostgreSQL test suite passes a unique temporary path to request
        # an isolated database. BaseDatabase maps that path to a deterministic
        # PostgreSQL schema; force its idempotent migration even when CI disables
        # automatic production migrations globally.
        if migrate is None and _is_legacy_path(url):
            migrate = True
        super().__init__(url, schema=schema, migrate=migrate)

    @contextmanager
    def connect(self) -> Iterator[_PsycopgConnectionAdapter]:
        # BaseDatabase owns transaction commit/rollback and connection cleanup.
        # Replace only the compatibility adapter so bulk writes use a cursor,
        # which is where psycopg3 implements executemany().
        with BaseDatabase.connect(self) as adapter:
            yield _PsycopgConnectionAdapter(adapter._connection)


__all__ = [
    "Database",
    "EMPLOYER_DATE_MODES",
    "TARGET_MATCHES",
    "application_identity",
    "coverage_role_signature",
    "iso",
]
