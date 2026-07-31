from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__version__ = "1.0.0"

# libpq rejects unknown URI query parameters before it attempts a connection.
# Hosting providers sometimes append dashboard/client metadata (for example
# ``supa=...``) to otherwise valid PostgreSQL URLs. Keep only parameters that
# libpq/psycopg understands so one provider-only tag cannot take every GAIA
# process offline.
_LIBPQ_URI_PARAMETERS = {
    "application_name",
    "channel_binding",
    "client_encoding",
    "connect_timeout",
    "dbname",
    "fallback_application_name",
    "gssencmode",
    "host",
    "hostaddr",
    "keepalives",
    "keepalives_count",
    "keepalives_idle",
    "keepalives_interval",
    "krbsrvname",
    "load_balance_hosts",
    "options",
    "passfile",
    "password",
    "port",
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
    "target_session_attrs",
    "tcp_user_timeout",
    "user",
}


def sanitize_postgres_url(value: str) -> str:
    """Return a stripped PostgreSQL URI containing only libpq parameters."""

    value = value.strip()
    if not value or "://" not in value:
        return value
    parts = urlsplit(value)
    if parts.scheme not in {"postgres", "postgresql"}:
        return value
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if key in _LIBPQ_URI_PARAMETERS
        ],
        doseq=True,
    )
    scheme = "postgresql" if parts.scheme == "postgres" else parts.scheme
    return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))


def _sanitize_database_environment() -> None:
    for name in (
        "GAIA_DATABASE_URL",
        "DATABASE_URL",
        "SUPABASE_DB_URL",
        "GAIA_TEST_DATABASE_URL",
        "GAIA_MIGRATION_DATABASE_URL",
    ):
        value = os.getenv(name)
        if value:
            os.environ[name] = sanitize_postgres_url(value)


_sanitize_database_environment()
