from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gaia import inventory_truth_api, product_api


def _live_stats() -> dict[str, int]:
    return {
        "active_listings": 97,
        "companies": 24,
        "validated_sources": 119,
    }


def _live_health() -> dict[str, object]:
    return {
        "ok": False,
        "stale": False,
        "progress": {"stage": "scheduled", "completed": 45, "total": 119},
        "data": {"last_run": {"status": "degraded"}},
        "inventory": {"healthy": False, "fresh": 45, "total": 119},
    }


def test_recovering_stats_remain_live_and_return_200(monkeypatch) -> None:
    app = FastAPI()
    monkeypatch.setattr(product_api, "live_stats", _live_stats)
    monkeypatch.setattr(product_api, "live_health", _live_health)
    inventory_truth_api.install_inventory_truth_api(app)

    response = TestClient(app).get("/api/stats")

    assert response.status_code == 200
    assert response.json()["active_listings"] == 97
    assert response.json()["inventory_complete"] is False
    assert response.json()["inventory_state"] == "recovering"


def test_recovering_health_is_degraded_not_stale_or_outage(monkeypatch) -> None:
    app = FastAPI()
    monkeypatch.setattr(product_api, "live_stats", _live_stats)
    monkeypatch.setattr(product_api, "live_health", _live_health)
    inventory_truth_api.install_inventory_truth_api(app)

    response = TestClient(app).get("/api/health")
    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is False
    assert payload["stale"] is False
    assert payload["reason"] == "inventory_recovery"
    assert payload["progress"]["stage"] == "inventory-recovery"
    assert payload["job_inventory"]["deficits"]["active_listings"] == 3
