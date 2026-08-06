from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gaia import runtime_discovery_api


def app() -> FastAPI:
    instance = FastAPI()
    runtime_discovery_api.install_runtime_discovery_api(instance)
    return instance


def test_runtime_discovery_rejects_public_requests(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_MARKET_DISCOVERY", "1")

    response = TestClient(app()).post("/api/maintenance/discover")

    assert response.status_code == 403


def test_runtime_discovery_accepts_production_scheduler(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_MARKET_DISCOVERY", "1")

    async def fake_discovery():
        return {
            "status": "ok",
            "executed": True,
            "summary": {
                "successful_queries": 8,
                "candidate_rows_written": 4,
                "candidate_sources_promoted": 2,
            },
        }

    monkeypatch.setattr(
        runtime_discovery_api,
        "run_runtime_market_discovery",
        fake_discovery,
    )
    response = TestClient(app()).post(
        "/api/maintenance/discover",
        headers={"User-Agent": "GAIA-production-maintenance/123"},
    )

    assert response.status_code == 200
    assert response.json()["summary"]["candidate_sources_promoted"] == 2


def test_disabled_runtime_discovery_is_not_exposed(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_ENABLE_RUNTIME_MARKET_DISCOVERY", "0")

    response = TestClient(app()).post(
        "/api/maintenance/discover",
        headers={"User-Agent": "GAIA-production-maintenance/123"},
    )

    assert response.status_code == 404


def test_runtime_discovery_is_leased_bounded_and_database_scheduled() -> None:
    source = Path("src/gaia/runtime_discovery_api.py").read_text(encoding="utf-8")
    scheduler = Path("src/gaia/database_scheduler.py").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/runtime-market-discovery.yml").read_text(
        encoding="utf-8"
    )

    assert "vercel-runtime-market-discovery" in source
    assert "run_dynamic_market_discovery" in source
    assert "asyncio.wait_for(" in source
    assert "GAIA_RUNTIME_MARKET_DISCOVERY_PROBE_LIMIT" in source
    assert "GAIA_RUNTIME_MARKET_DISCOVERY_TIMEOUT_SECONDS" in source
    assert "publish_committed_updates" in source
    assert "candidate_sources_promoted" in source
    assert 'schedule="3,18,33,48 * * * *"' in scheduler
    assert "/api/maintenance/discover" in scheduler

    # The old GitHub/Vercel workflow stays manual. Supabase owns the production clock,
    # avoiding two independent scheduled loops and the CPU burn that caused the outage.
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "push:" not in workflow
