from __future__ import annotations

import importlib
import os

import gaia


def test_sanitize_postgres_url_removes_provider_metadata() -> None:
    value = (
        "postgresql://user:pass@example.supabase.com:5432/postgres"
        "?sslmode=require&supa=base-pooler.example&connect_timeout=12"
    )

    sanitized = gaia.sanitize_postgres_url(value)

    assert sanitized.startswith("postgresql://user:pass@example.supabase.com:5432/postgres?")
    assert "sslmode=require" in sanitized
    assert "connect_timeout=12" in sanitized
    assert "supa=" not in sanitized


def test_sanitize_postgres_url_normalizes_scheme_and_whitespace() -> None:
    assert (
        gaia.sanitize_postgres_url("  postgres://u:p@host/db?sslmode=require  ")
        == "postgresql://u:p@host/db?sslmode=require"
    )


def test_sanitize_postgres_url_leaves_non_database_urls_alone() -> None:
    value = "https://example.com/path?supa=value"
    assert gaia.sanitize_postgres_url(value) == value


def test_import_sanitizes_all_database_environment_variables(monkeypatch) -> None:
    dirty = "postgresql://u:p@host/db?sslmode=require&supa=metadata"
    for name in (
        "GAIA_DATABASE_URL",
        "DATABASE_URL",
        "SUPABASE_DB_URL",
        "GAIA_TEST_DATABASE_URL",
        "GAIA_MIGRATION_DATABASE_URL",
    ):
        monkeypatch.setenv(name, dirty)

    importlib.reload(gaia)

    for name in (
        "GAIA_DATABASE_URL",
        "DATABASE_URL",
        "SUPABASE_DB_URL",
        "GAIA_TEST_DATABASE_URL",
        "GAIA_MIGRATION_DATABASE_URL",
    ):
        assert os.environ[name] == "postgresql://u:p@host/db?sslmode=require"
