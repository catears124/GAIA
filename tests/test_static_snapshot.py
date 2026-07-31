from __future__ import annotations

import json
from pathlib import Path

from gaia import static_snapshot


def test_snapshot_key_is_stable_and_omits_default_values() -> None:
    assert static_snapshot._key("/api/families", page=1, remote=False, q="") == "/api/families?page=1"
    assert (
        static_snapshot._key(
            "/api/families",
            trust="verified",
            category="quant",
            target="default",
        )
        == "/api/families?category=quant&target=default&trust=verified"
    )


def test_snapshot_writer_is_atomic_and_contains_required_routes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        static_snapshot,
        "live_health",
        lambda: {"inventory": {"latest_activity_at": "2026-07-31T00:00:00+00:00"}},
    )
    monkeypatch.setattr(static_snapshot, "live_stats", lambda: {"active_listings": 2})
    monkeypatch.setattr(
        static_snapshot,
        "live_facets",
        lambda trust="all", target="": {"companies": [], "trust": trust, "target": target},
    )
    monkeypatch.setattr(
        static_snapshot,
        "live_families",
        lambda **kwargs: {"items": [{"family_key": "one"}], "total": 1, "page": kwargs["page"]},
    )

    output = tmp_path / "snapshot.json"
    assert static_snapshot.write_snapshot(output) == output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["generated_at"]
    assert payload["source_activity_at"] == "2026-07-31T00:00:00+00:00"
    assert payload["responses"]["/api/health"]
    assert payload["responses"]["/api/stats"]
    assert payload["responses"]["/api/families"]["items"]
    assert not output.with_suffix(".json.tmp").exists()


def test_snapshot_contains_common_first_visit_searches(monkeypatch) -> None:
    monkeypatch.setattr(static_snapshot, "live_health", lambda: {"inventory": {}})
    monkeypatch.setattr(static_snapshot, "live_stats", lambda: {})
    monkeypatch.setattr(static_snapshot, "live_facets", lambda trust="all", target="": {})
    monkeypatch.setattr(static_snapshot, "live_families", lambda **kwargs: {"items": [], "total": 0})

    responses = static_snapshot.build_snapshot()["responses"]
    assert "/api/families" in responses
    assert "/api/families?page=5" in responses
    assert "/api/families?posted_within=1&trust=verified" in responses
    assert "/api/families?category=quant&target=default&trust=verified" in responses
    assert "/api/families?remote=true&trust=verified" in responses
