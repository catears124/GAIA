from __future__ import annotations

import os
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
    application_identity,
    coverage_role_signature,
    iso,
)
from .db_read import ReadMixin  # noqa: E402
from .db_write import WriteMixin  # noqa: E402


class Database(WriteMixin, ReadMixin, BaseDatabase):
    """GAIA's PostgreSQL repository and query service."""


__all__ = [
    "Database",
    "EMPLOYER_DATE_MODES",
    "TARGET_MATCHES",
    "application_identity",
    "coverage_role_signature",
    "iso",
]
