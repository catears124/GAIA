from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
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
    """Connection adapter with psycopg3 cursor-backed bulk execution."""

    def executemany(self, query: str, params_seq: Any) -> None:
        with self._connection.cursor() as cursor:
            cursor.executemany(self._query(query), params_seq)


class Database(WriteMixin, ReadMixin, BaseDatabase):
    """GAIA's PostgreSQL repository and query service."""

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
