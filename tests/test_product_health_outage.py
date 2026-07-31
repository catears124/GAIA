from __future__ import annotations

import json

import psycopg

from gaia import product_api


def test_health_database_uses_bounded_isolated_timeout(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, *, url, schema, migrate):
            self.url = url
            self.schema = schema
            self.migrate = migrate
            self.timeout = 99

    monkeypatch.setattr(product_api, "Database", FakeDatabase)
    monkeypatch.setenv("GAIA_HEALTH_DB_TIMEOUT", "60")

    database = product_api._health_database()

    assert database.url == product_api.legacy.db.url
    assert database.schema == product_api.legacy.db.schema
    assert database.migrate is False
    assert database.timeout == 10


def test_health_database_timeout_has_one_second_floor(monkeypatch) -> None:
    class FakeDatabase:
        def __init__(self, **_kwargs):
            self.timeout = 99

    monkeypatch.setattr(product_api, "Database", FakeDatabase)
    monkeypatch.setenv("GAIA_HEALTH_DB_TIMEOUT", "0")

    assert product_api._health_database().timeout == 1


def test_live_health_returns_truthful_cache_resistant_503(monkeypatch) -> None:
    monkeypatch.setattr(product_api, "_health_database", lambda: object())

    def unavailable(_database):
        raise psycopg.OperationalError("database is restarting")

    monkeypatch.setattr(product_api, "inventory_state", unavailable)

    response = product_api.live_health()
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert payload["ok"] is False
    assert payload["stale"] is True
    assert payload["reason"] == "database_unavailable"
    assert payload["progress"]["stage"] == "database-recovery"
    assert payload["inventory"]["total"] == 0
    assert payload["inventory"]["healthy"] is False


def test_live_health_marks_successful_evidence_non_stale(monkeypatch) -> None:
    monkeypatch.setattr(product_api, "_health_database", lambda: object())
    monkeypatch.setattr(
        product_api,
        "inventory_state",
        lambda _database: {
            "total": 5,
            "fresh": 5,
            "unhealthy": 0,
            "running": 0,
            "never_completed": 0,
            "overdue": 0,
            "degraded": 0,
            "healthy": True,
            "coverage_watermark": "2026-07-31T18:00:00+00:00",
        },
    )

    payload = product_api.live_health()

    assert isinstance(payload, dict)
    assert payload["ok"] is True
    assert payload["stale"] is False
    assert payload["data"]["sources"] == 5
