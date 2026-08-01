from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gaia import conversion_diagnostics_api


def app() -> FastAPI:
    instance = FastAPI()
    conversion_diagnostics_api.install_conversion_diagnostics_api(instance)
    return instance


def test_conversion_diagnostics_rejects_public_requests(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_CONVERSION_DIAGNOSTICS", "1")

    response = TestClient(app()).get("/api/maintenance/diagnostics/conversion")

    assert response.status_code == 403


def test_disabled_conversion_diagnostics_is_not_exposed(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_CONVERSION_DIAGNOSTICS", "0")

    response = TestClient(app()).get(
        "/api/maintenance/diagnostics/conversion",
        headers={"User-Agent": "GAIA-production-maintenance/123"},
    )

    assert response.status_code == 404


def test_failure_counts_groups_real_reasons() -> None:
    rows = [
        {"last_error": "timeout", "status": "retry"},
        {"last_error": "timeout", "status": "retry"},
        {"rejection_reason": "no relevant jobs", "status": "rejected"},
        {"status": "candidate"},
        {"diagnostic_error": "older schema"},
    ]

    assert conversion_diagnostics_api._failure_counts(rows) == [
        {"reason": "timeout", "count": 2},
        {"reason": "no relevant jobs", "count": 1},
        {"reason": "candidate", "count": 1},
    ]


def test_diagnostics_installation_is_idempotent() -> None:
    instance = app()
    conversion_diagnostics_api.install_conversion_diagnostics_api(instance)

    paths = [route.path for route in instance.routes]
    assert paths.count("/api/maintenance/diagnostics/conversion") == 1
