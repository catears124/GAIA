from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from gaia import db as db_module


DATABASE_VARIABLES = (
    "GAIA_DATABASE_URL",
    "POSTGRES_URL",
    "DATABASE_URL",
    "SUPABASE_DB_URL",
)


def _clear_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in DATABASE_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def test_standard_supabase_postgres_url_needs_no_gaia_specific_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_environment(monkeypatch)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "postgres://postgres.ref:secret@pooler.supabase.com:6543/postgres?supa=x&sslmode=require",
    )

    resolved = db_module._configured_database_url()  # noqa: SLF001

    assert resolved is not None
    assert resolved.startswith("postgres://")
    assert "supa=" not in resolved
    assert parse_qs(urlsplit(resolved).query)["sslmode"] == ["require"]


def test_database_construction_remains_safe_without_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_environment(monkeypatch)

    database = db_module.Database(migrate=False)

    assert database.url is None
    with pytest.raises(RuntimeError, match="PostgreSQL is not configured"):
        with database.connect():
            pass


def test_vercel_entrypoint_performs_no_database_work_during_build_import() -> None:
    source = Path("app.py").read_text(encoding="utf-8")

    assert 'os.environ.setdefault("GAIA_AUTO_MIGRATE", "0")' in source
    assert "add_event_handler" not in source
    assert "runtime_bootstrap" not in source
    assert "install_request_bootstrap(app)" in source
    assert source.index("install_request_bootstrap(app)") < source.index(
        "install_database_outage_guard(app)"
    )


def test_request_bootstrap_lazy_loads_heavy_recovery_code() -> None:
    source = Path("src/gaia/request_bootstrap.py").read_text(encoding="utf-8")

    assert "from .runtime_bootstrap import bootstrap_empty_database" in source
    assert source.index("async def _ensure_runtime_database") < source.index(
        "from .runtime_bootstrap import bootstrap_empty_database"
    )
    assert "request.url.path.startswith(\"/api/\")" in source
    assert "_NEXT_ATTEMPT_AT" in source
    assert "asyncio.Lock()" in source


def test_runtime_bootstrap_uses_only_existing_supabase_variables_and_is_bounded() -> None:
    source = Path("src/gaia/runtime_bootstrap.py").read_text(encoding="utf-8")
    cli_source = Path("src/gaia/cli.py").read_text(encoding="utf-8")

    assert "from .cli import run_migration" in source
    assert "database.migrate()" in source
    assert "POSTGRES_URL_NON_POOLING" in cli_source
    assert "GAIA_BOOTSTRAP_BUDGET_SECONDS" in source
    assert "asyncio.wait_for" in source
    assert "include_universe=False" in source
    assert "database.rebuild_families()" in source


def test_recreated_database_is_seeded_and_leased_before_registry_recovery() -> None:
    source = Path("src/gaia/runtime_bootstrap.py").read_text(encoding="utf-8")

    assert "google-careers" in source
    assert "market-discovery" in source
    assert "empty-database-bootstrap" in source
    assert "lease_expires_at" in source
    assert "postings" in source and "families" in source
