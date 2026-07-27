from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# Vercel's Supabase integration provides POSTGRES_URL automatically. Normalize it
# into GAIA's explicit name before importing the database implementation so local,
# Vercel, and standalone Supabase environments share one code path.
if not os.getenv("GAIA_DATABASE_URL"):
    for variable in ("POSTGRES_URL", "DATABASE_URL", "SUPABASE_DB_URL"):
        if value := os.getenv(variable):
            os.environ["GAIA_DATABASE_URL"] = value
            break

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
