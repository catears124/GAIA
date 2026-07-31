from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from gaia import static_snapshot_http


class _FakeClient:
    def __init__(self, payloads: dict[str, dict[str, Any]], **_: Any) -> None:
        self.payloads = payloads

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, path: str, params: dict[str, object] | None = None) -> httpx.Response:
        params = params or {}
        key = path
        if path == "/api/families":
            key = f"{path}:page={params.get('page', 1)}:size={params.get('page_size', 48)}"
        payload = self.payloads.get(key, self.payloads.get(path))
        request = httpx.Request("GET", f"https://example.test{path}")
        if payload is None:
            return httpx.Response(404, request=request, json={"error": "missing fixture"})
        return httpx.Response(200, request=request, json=payload)


def _family(key: str) -> dict[str, object]:
    return {
        "family_key": key,
        "title": f"Software Intern {key}",
        "company": "Example",
        "category": "software",
        "target_match": "exact",
        "year": 2027,
        "season": "Summer",
        "locations": ["Remote"],
        "opening_count": 1,
        "verified": True,
        "latest_posted_at": "2026-07-30T00:00:00Z",
        "first_detected_at": "2026-07-30T01:00:00Z",
        "last_verified_at": "2026-07-30T02:00:00Z",
        "openings": [{"apply_url": f"https://example.test/{key}", "source": "employer"}],
    }


def test_public_api_snapshot_requires_truthful_healthy_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = {
        "/api/health": {"ok": True, "stale": False, "inventory": {"healthy": False}},
    }
    monkeypatch.setattr(static_snapshot_http.httpx, "Client", lambda **kwargs: _FakeClient(payloads, **kwargs))
    with pytest.raises(static_snapshot_http.SnapshotExportError, match="unhealthy or stale"):
        static_snapshot_http.build_snapshot("https://example.test")


def test_public_api_snapshot_exports_complete_compact_index(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_family("a"), _family("b")]
    payloads = {
        "/api/health": {
            "ok": True,
            "stale": False,
            "inventory": {"healthy": True, "latest_activity_at": "2026-07-30T02:00:00Z"},
        },
        "/api/stats": {"active_listings": 2, "companies": 1, "new_today": 2},
        "/api/facets": {"companies": [], "categories": [], "remote_count": 2},
        "/api/families": {"items": rows, "total": 2, "page": 1, "page_size": 48},
        "/api/families:page=1:size=100": {"items": rows, "total": 2, "page": 1, "page_size": 100},
        "/api/families:page=2:size=48": {"items": [], "total": 2, "page": 2, "page_size": 48},
        "/api/families:page=3:size=48": {"items": [], "total": 2, "page": 3, "page_size": 48},
        "/api/families:page=4:size=48": {"items": [], "total": 2, "page": 4, "page_size": 48},
        "/api/families:page=5:size=48": {"items": [], "total": 2, "page": 5, "page_size": 48},
    }
    monkeypatch.setattr(static_snapshot_http.httpx, "Client", lambda **kwargs: _FakeClient(payloads, **kwargs))
    snapshot = static_snapshot_http.build_snapshot("https://example.test")
    assert snapshot["export_source"] == "public-api"
    assert snapshot["family_index_complete"] is True
    assert snapshot["family_index_total"] == 2
    assert [item["family_key"] for item in snapshot["family_index"]] == ["a", "b"]
    assert "description" not in snapshot["family_index"][0]


def test_workflows_suppress_database_fanout_and_preserve_usable_snapshot() -> None:
    maintenance = Path(".github/workflows/maintenance.yml").read_text()
    snapshot = Path(".github/workflows/static-snapshot.yml").read_text()
    assert "Gate maintenance on database readiness" in maintenance
    assert "Database recovery active; production maintenance suppressed" in maintenance
    assert "if: needs.readiness.outputs.ready == 'true'" in maintenance
    assert "python -m gaia.static_snapshot_http" in snapshot
    assert "retained snapshot remains usable" in snapshot
    assert "retention-days: 30" in snapshot
