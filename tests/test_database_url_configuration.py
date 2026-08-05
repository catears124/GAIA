from urllib.parse import parse_qs, urlsplit

from psycopg.conninfo import conninfo_to_dict

from gaia import db as db_module


def test_prisma_metadata_is_removed_before_psycopg_parses_url() -> None:
    raw = (
        "postgresql://postgres.project:password@pooler.supabase.com:6543/postgres"
        "?pgbouncer=true&connection_limit=1&pool_timeout=10"
        "&supa=base-pooler.x&sslmode=require&connect_timeout=15"
    )

    normalized = db_module._normalize_database_url(raw)

    assert parse_qs(urlsplit(normalized).query) == {
        "connect_timeout": ["15"],
        "sslmode": ["require"],
    }
    assert conninfo_to_dict(normalized)["dbname"] == "postgres"


def test_explicit_gaia_database_url_wins_over_integration_aliases(monkeypatch) -> None:
    override = "postgresql://override:password@override.example/postgres?sslmode=require"
    integration = (
        "postgresql://integration:password@integration.example/postgres"
        "?pgbouncer=true&sslmode=require"
    )
    monkeypatch.setenv("GAIA_DATABASE_URL", override)
    monkeypatch.setenv("POSTGRES_PRISMA_URL", integration)

    assert db_module._configured_database_url() == override
