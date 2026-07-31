from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from gaia.db import Database

_DATABASE_VARIABLES = (
    "GAIA_DATABASE_URL",
    "POSTGRES_URL",
    "DATABASE_URL",
    "SUPABASE_DB_URL",
)


def _without_database_configuration() -> dict[str, str]:
    environment = os.environ.copy()
    for variable in _DATABASE_VARIABLES:
        environment[variable] = ""
    environment["GAIA_AUTO_MIGRATE"] = "0"
    return environment


def test_asgi_application_import_does_not_require_database_credentials() -> None:
    repository = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app import app; assert app.title == 'GAIA'",
        ],
        cwd=repository,
        env=_without_database_configuration(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_database_requires_configuration_only_when_used(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in _DATABASE_VARIABLES:
        monkeypatch.delenv(variable, raising=False)

    database = Database(migrate=False)

    assert database.url is None
    with pytest.raises(RuntimeError, match="PostgreSQL is not configured"):
        with database.connect():
            pass


def test_explicit_database_url_remains_eagerly_available() -> None:
    database = Database(
        url="postgresql://example:example@localhost:5432/gaia",
        migrate=False,
    )

    assert database.url == "postgresql://example:example@localhost:5432/gaia"
