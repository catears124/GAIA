from __future__ import annotations

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gaia.api_resilience import install_database_outage_guard, is_database_outage


def test_database_outage_recognition_is_narrow() -> None:
    assert is_database_outage(psycopg.OperationalError("restart"))
    assert is_database_outage(TimeoutError("deadline"))
    assert is_database_outage(OSError("network"))
    assert is_database_outage(RuntimeError("PostgreSQL is not configured. Set URL"))
    assert not is_database_outage(RuntimeError("application invariant failed"))


def test_database_failure_returns_explicit_503() -> None:
    application = FastAPI()
    install_database_outage_guard(application)

    @application.get("/api/stats")
    def stats() -> dict[str, object]:
        raise psycopg.OperationalError("restart")

    response = TestClient(application, raise_server_exceptions=False).get("/api/stats")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["retry-after"] == "30"
    assert response.json() == {
        "ok": False,
        "stale": True,
        "reason": "database_unavailable",
        "endpoint": "stats",
        "detail": "OperationalError",
    }


def test_non_api_failure_is_not_reclassified() -> None:
    application = FastAPI()
    install_database_outage_guard(application)

    @application.get("/")
    def root() -> dict[str, object]:
        raise psycopg.OperationalError("restart")

    response = TestClient(application, raise_server_exceptions=False).get("/")
    assert response.status_code == 500
