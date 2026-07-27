from __future__ import annotations

from .db_base import (
    EMPLOYER_DATE_MODES,
    TARGET_MATCHES,
    BaseDatabase,
    application_identity,
    coverage_role_signature,
    iso,
)
from .db_read import ReadMixin
from .db_write import WriteMixin


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
