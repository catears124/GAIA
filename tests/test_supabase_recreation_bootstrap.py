from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from gaia import db as db_module


DATABASE_VARIABLES = (
    "GAIA_DATABASE_URL",
    "GAIA_MIGRATION_DATABASE_URL",
    "GAIA_ADMIN_DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL_NON_POOLING",
    "DATABASE_URL",
    "SUPABASE_DB_URL",
    "POSTGRES_HOST",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
)


def _clear_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in DATABASE_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def test_standard_postgres_url_is_used_without_gaia_specific_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_environment(monkeypatch)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "postgres://postgres.ref:secret@pooler.supabase.com:6543/postgres?supa=x",
    )

    resolved = db_module._configured_database_url()  # noqa: SLF001

    assert resolved is not None
    assert resolved.startswith("postgresql://")
    assert "supa=" not in resolved
    assert parse_qs(urlsplit(resolved).query)["sslmode"] == ["require"]


def test_migrations_prefer_the_non_pooling_supabase_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_environment(monkeypatch)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://pooler/db")
    monkeypatch.setenv("POSTGRES_URL_NON_POOLING", "postgresql://direct/db")

    assert db_module._configured_database_url(migration=True) == "postgresql://direct/db"  # noqa: SLF001
    assert db_module._configured_database_url() == "postgresql://pooler/db"  # noqa: SLF001


def test_prisma_only_url_is_accepted_and_prisma_parameters_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_environment(monkeypatch)
    monkeypatch.setenv(
        "POSTGRES_PRISMA_URL",
        "postgresql://user:pass@pooler.supabase.com/db?pgbouncer=true&connection_limit=1",
    )

    resolved = db_module._configured_database_url()  # noqa: SLF001
    query = parse_qs(urlsplit(resolved or "").query)

    assert resolved is not None
    assert "pgbouncer" not in query
    assert "connection_limit" not in query
    assert query["sslmode"] == ["require"]


def test_component_variables_build_a_url_with_escaped_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_environment(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "db.example.supabase.co:5432")
    monkeypatch.setenv("POSTGRES_USER", "postgres.project")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:/word")
    monkeypatch.setenv("POSTGRES_DATABASE", "postgres")

    resolved = db_module._configured_database_url()  # noqa: SLF001

    assert resolved is not None
    assert "p%40ss%3A%2Fword" in resolved
    assert resolved.endswith("/postgres?sslmode=require")


def test_database_construction_remains_safe_without_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_environment(monkeypatch)

    database = db_module.Database(migrate=False)

    assert database.url is None
    with pytest.raises(RuntimeError, match="PostgreSQL is not configured"):
        with database.connect():
            pass


def test_vercel_entrypoint_enables_locked_migration_and_empty_database_recovery() -> None:
    source = Path("app.py").read_text(encoding="utf-8")

    assert 'os.environ.setdefault("GAIA_AUTO_MIGRATE", "1")' in source
    assert 'os.environ.setdefault("GAIA_BOOTSTRAP_EMPTY_DATABASE", "1")' in source
    assert "install_runtime_bootstrap(app)" in source
    assert source.index("install_runtime_bootstrap(app)") < source.index(
        "install_database_outage_guard(app)"
    )


def test_runtime_bootstrap_is_bounded_leased_and_materializes_registry_rows() -> None:
    source = Path("src/gaia/runtime_bootstrap.py").read_text(encoding="utf-8")

    assert "lease_expires_at" in source
    assert "GAIA_BOOTSTRAP_BUDGET_SECONDS" in source
    assert "asyncio.wait_for" in source
    assert "include_universe=False" in source
    assert "database.rebuild_families()" in source
    assert "postings" in source and "families" in source


def test_runtime_migration_is_fingerprinted_and_advisory_locked() -> None:
    source = Path("src/gaia/db.py").read_text(encoding="utf-8")

    assert "gaia_schema_state" in source
    assert "pg_advisory_xact_lock" in source
    assert "POSTGRES_URL_NON_POOLING" in source
    assert "runtime-bootstrap" in source
    assert "market-discovery" in source
