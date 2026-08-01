from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gaia import conversion_diagnostics_api
from gaia.db import Database


def app() -> FastAPI:
    instance = FastAPI()
    conversion_diagnostics_api.install_conversion_diagnostics_api(instance)
    return instance


def test_conversion_report_runs_against_current_schema(tmp_path) -> None:
    database = Database(tmp_path / "conversion-funnel.db")
    try:
        report = conversion_diagnostics_api.build_report(
            database,
            hours=24,
            limit=25,
        )
    finally:
        database.drop_schema()

    assert report["objective"] == "increase_verified_new_jobs"
    assert report["diagnostic_errors"] == []
    assert report["funnel"]["new_verified_jobs_window"] == 0
    assert report["sources"]["candidate_sources_due"] == 0


def test_conversion_diagnostics_rejects_public_requests(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_CONVERSION_DIAGNOSTICS", "1")
    assert TestClient(app()).get("/api/maintenance/diagnostics/conversion").status_code == 403


def test_disabled_conversion_diagnostics_is_not_exposed(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_CONVERSION_DIAGNOSTICS", "0")
    response = TestClient(app()).get(
        "/api/maintenance/diagnostics/conversion",
        headers={"User-Agent": "GAIA-production-maintenance/123"},
    )
    assert response.status_code == 404


def test_candidate_drain_is_authenticated_and_bounded(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_CONVERSION_DIAGNOSTICS", "1")
    captured = {}

    async def fake_drain(*, limit, concurrency, hours):
        captured.update(limit=limit, concurrency=concurrency, hours=hours)
        return {"status": "ok", "claimed_candidates": 12, "promoted_sources": 3}

    monkeypatch.setattr(conversion_diagnostics_api, "drain_candidates", fake_drain)
    response = TestClient(app()).post(
        "/api/maintenance/diagnostics/drain-candidates?limit=999&concurrency=999&hours=99999",
        headers={"User-Agent": "GAIA-production-maintenance/123"},
    )
    assert response.status_code == 200
    assert response.json()["promoted_sources"] == 3
    assert captured == {"limit": 64, "concurrency": 12, "hours": 720}


def test_failure_counts_groups_actionable_classes() -> None:
    rows = [
        {"last_error": "HTTP 403"},
        {"last_error": "rate limit exceeded"},
        {"last_error": "ReadTimeout"},
        {"target_match": "wrong_year"},
    ]
    counts = conversion_diagnostics_api.failure_counts(rows)
    assert counts[0]["reason"] == "blocked_or_rate_limited"
    assert counts[0]["count"] == 2
    assert {row["reason"] for row in counts} >= {"timeout", "classification_rejected"}


def test_diagnostics_installation_is_idempotent() -> None:
    instance = app()
    conversion_diagnostics_api.install_conversion_diagnostics_api(instance)
    paths = [route.path for route in instance.routes]
    assert paths.count("/api/maintenance/diagnostics/conversion") == 1
    assert paths.count("/api/maintenance/diagnostics/drain-candidates") == 1
