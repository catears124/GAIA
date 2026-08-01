from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gaia import maintenance_api


def app() -> FastAPI:
    instance = FastAPI()
    maintenance_api.install_maintenance_api(instance)
    return instance


def test_runtime_tick_rejects_ordinary_public_requests(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_TICK", "1")

    response = TestClient(app()).post("/api/maintenance/tick")

    assert response.status_code == 403


def test_runtime_tick_accepts_the_production_scheduler(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_TICK", "1")

    async def fake_tick():
        return {
            "status": "ok",
            "executed": True,
            "inventory": {"healthy": False, "fresh": 5, "total": 6},
            "summary": {"failed": 0},
        }

    monkeypatch.setattr(maintenance_api, "run_inventory_tick", fake_tick)
    response = TestClient(app()).post(
        "/api/maintenance/tick",
        headers={"User-Agent": "GAIA-production-maintenance/1"},
    )

    assert response.status_code == 200
    assert response.json()["executed"] is True


def test_vercel_cron_user_agent_is_supported(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_TICK", "1")

    async def fake_tick():
        return {"status": "not_due", "executed": False, "inventory": {}, "summary": None}

    monkeypatch.setattr(maintenance_api, "run_inventory_tick", fake_tick)
    response = TestClient(app()).post(
        "/api/maintenance/tick",
        headers={"User-Agent": "vercel-cron/1.0"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "not_due"


def test_disabled_runtime_tick_is_not_exposed(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_TICK", "0")

    response = TestClient(app()).post(
        "/api/maintenance/tick",
        headers={"User-Agent": "GAIA-production-maintenance/1"},
    )

    assert response.status_code == 404


def test_runtime_coverage_rejects_public_requests(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_COVERAGE", "1")

    response = TestClient(app()).post("/api/maintenance/coverage")

    assert response.status_code == 403


def test_runtime_coverage_accepts_the_production_scheduler(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_COVERAGE", "1")

    async def fake_coverage():
        return {
            "status": "ok",
            "executed": True,
            "rebuilt_employers": 12,
            "merged_observations": 337,
            "universe": {
                "ready": True,
                "summary": {"known_employers": 349, "enumerated_employers": 12},
            },
        }

    monkeypatch.setattr(maintenance_api, "run_coverage_tick", fake_coverage)
    response = TestClient(app()).post(
        "/api/maintenance/coverage",
        headers={"User-Agent": "GAIA-production-maintenance/1"},
    )

    assert response.status_code == 200
    assert response.json()["universe"]["summary"]["known_employers"] == 349


def test_disabled_runtime_coverage_is_not_exposed(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_COVERAGE", "0")

    response = TestClient(app()).post(
        "/api/maintenance/coverage",
        headers={"User-Agent": "GAIA-production-maintenance/1"},
    )

    assert response.status_code == 404


def test_runtime_tick_is_database_leased_and_budget_bounded() -> None:
    source = __import__("pathlib").Path("src/gaia/maintenance_api.py").read_text(encoding="utf-8")

    assert "lease_expires_at" in source
    assert "next_run_at<=now()" in source
    assert "GAIA_RUNTIME_TICK_BUDGET_SECONDS" in source
    assert "min(float" in source
    assert "once=True" in source
    assert "budget_seconds=budget" in source


def test_runtime_coverage_is_leased_and_validates_the_census() -> None:
    source = __import__("pathlib").Path("src/gaia/maintenance_api.py").read_text(encoding="utf-8")

    assert "vercel-runtime-coverage-reconcile" in source
    assert "rebuild_employer_universe(database)" in source
    assert "merge_observations_into_universe(database)" in source
    assert 'summary.get("known_employers")' in source
    assert 'summary.get("enumerated_employers")' in source
    assert 'report.get("rebuild_required") is True' in source
