from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gaia import inventory_truth_api


def test_classify_inventory_reports_exact_deficits(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_BOOTSTRAP_MIN_ACTIVE_APPLICATIONS", "100")
    monkeypatch.setenv("GAIA_BOOTSTRAP_MIN_ACTIVE_COMPANIES", "20")
    monkeypatch.setenv("GAIA_BOOTSTRAP_MIN_VALIDATED_SOURCES", "25")

    recovery = inventory_truth_api.classify_inventory(
        {"active_listings": 19, "companies": 5, "validated_sources": 19}
    )

    assert recovery == {
        "complete": False,
        "state": "recovering",
        "completion_percent": 19.0,
        "observed": {
            "active_listings": 19,
            "companies": 5,
            "validated_sources": 19,
        },
        "thresholds": {
            "active_listings": 100,
            "companies": 20,
            "validated_sources": 25,
        },
        "deficits": {
            "active_listings": 81,
            "companies": 15,
            "validated_sources": 6,
        },
    }


def test_health_cannot_be_green_for_collapsed_job_inventory(monkeypatch) -> None:
    app = FastAPI()
    monkeypatch.setattr(
        inventory_truth_api.product_api,
        "live_stats",
        lambda: {
            "active_listings": 19,
            "companies": 5,
            "validated_sources": 19,
            "new_today": 4,
        },
    )
    monkeypatch.setattr(
        inventory_truth_api.product_api,
        "live_health",
        lambda: {
            "ok": True,
            "stale": False,
            "progress": {"stage": "scheduled"},
            "data": {"last_run": {"status": "ok"}},
        },
    )

    inventory_truth_api.install_inventory_truth_api(app)
    client = TestClient(app)

    stats_response = client.get("/api/stats")
    assert stats_response.status_code == 200
    stats = stats_response.json()
    assert stats["inventory_complete"] is False
    assert stats["inventory_state"] == "recovering"

    health_response = client.get("/api/health")
    assert health_response.status_code == 503
    assert health_response.headers["cache-control"] == "no-store, max-age=0"
    assert health_response.headers["retry-after"] == "30"
    health = health_response.json()
    assert health["ok"] is False
    assert health["stale"] is True
    assert health["reason"] == "inventory_recovery"
    assert health["progress"] == {
        "stage": "inventory-recovery",
        "completed": 19,
        "total": 100,
    }
    assert health["data"]["last_run"]["status"] == "partial"
    assert health["job_inventory"]["deficits"]["active_listings"] == 81


def test_complete_inventory_preserves_real_source_health(monkeypatch) -> None:
    app = FastAPI()
    monkeypatch.setattr(
        inventory_truth_api.product_api,
        "live_stats",
        lambda: {
            "active_listings": 200,
            "companies": 40,
            "validated_sources": 50,
        },
    )
    monkeypatch.setattr(
        inventory_truth_api.product_api,
        "live_health",
        lambda: {"ok": False, "stale": False, "reason": "source_failures"},
    )

    inventory_truth_api.install_inventory_truth_api(app)
    response = TestClient(app).get("/api/health")
    health = response.json()

    assert response.status_code == 200
    assert health["ok"] is False
    assert health["reason"] == "source_failures"
    assert health["job_inventory"]["complete"] is True
    assert health["job_inventory"]["completion_percent"] == 100.0
