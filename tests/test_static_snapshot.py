from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    monkeypatch.setenv("GAIA_STATIC_SNAPSHOT_MIN_ACTIVE_LISTINGS", "1")
    monkeypatch.setattr(
        static_snapshot,
        "live_health",
        lambda: {"inventory": {"latest_activity_at": "2026-07-31T00:00:00+00:00"}},
    )
    monkeypatch.setattr(
        static_snapshot,
        "live_stats",
        lambda: {"active_listings": 2, "companies": 1, "new_today": 1},
    )
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
    assert payload["schema_version"] == 2
    assert payload["generated_at"]
    assert payload["source_activity_at"] == "2026-07-31T00:00:00+00:00"
    assert payload["responses"]["/api/health"]
    assert payload["responses"]["/api/stats"]
    assert payload["responses"]["/api/families"]["items"]
    assert payload["family_index"] == [{"family_key": "one", "openings": []}]
    assert payload["family_index_total"] == 1
    assert payload["family_index_complete"] is True
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


def test_compact_family_keeps_search_detail_and_apply_fields_only() -> None:
    compact = static_snapshot._compact_family(
        {
            "family_key": "family",
            "title": "Software Intern",
            "company": "Example",
            "target_match": "exact",
            "description": "large field that should not ship",
            "internal_debug": {"large": True},
            "openings": [
                {
                    "apply_url": "https://example.com/apply",
                    "source_mode": "direct",
                    "location": ["Remote"],
                    "description": "large opening description",
                    "raw_payload": {"large": True},
                }
            ],
        }
    )

    assert compact["family_key"] == "family"
    assert compact["target_match"] == "exact"
    assert "description" not in compact
    assert "internal_debug" not in compact
    assert compact["openings"] == [
        {
            "apply_url": "https://example.com/apply",
            "source_mode": "direct",
            "location": ["Remote"],
        }
    ]


def test_family_index_paginates_until_every_visible_family_is_exported(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []

    def families(**kwargs: object) -> dict[str, object]:
        page = int(kwargs["page"])
        page_size = int(kwargs["page_size"])
        calls.append((page, page_size))
        start = (page - 1) * page_size
        total = 205
        rows = [
            {"family_key": f"family-{index}", "title": f"Role {index}"}
            for index in range(start, min(start + page_size, total))
        ]
        return {"items": rows, "total": total, "page": page}

    monkeypatch.setattr(static_snapshot, "live_families", families)
    items, total, complete = static_snapshot._family_index()

    assert calls == [(1, 100), (2, 100), (3, 100)]
    assert total == 205
    assert complete is True
    assert len(items) == 205
    assert items[-1]["family_key"] == "family-204"


def test_family_index_marks_snapshot_incomplete_when_safety_cap_is_reached(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_STATIC_SNAPSHOT_MAX_PAGES", "2")
    monkeypatch.setattr(
        static_snapshot,
        "live_families",
        lambda **kwargs: {
            "items": [
                {"family_key": f"{kwargs['page']}-{index}"}
                for index in range(100)
            ],
            "total": 500,
        },
    )

    items, total, complete = static_snapshot._family_index()
    assert len(items) == 200
    assert total == 500
    assert complete is False


def _snapshot(active: int, companies: int, new_today: int, *, complete: bool = True) -> dict[str, object]:
    return {
        "family_index": [{}] * active,
        "family_index_total": active,
        "family_index_complete": complete,
        "responses": {
            "/api/stats": {
                "active_listings": active,
                "companies": companies,
                "new_today": new_today,
            }
        },
    }


def test_snapshot_rejects_impossible_homepage_metrics(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_STATIC_SNAPSHOT_MIN_ACTIVE_LISTINGS", "1")
    with pytest.raises(RuntimeError, match="new_today=29 exceeds active_listings=19"):
        static_snapshot._validate_snapshot(_snapshot(19, 5, 29))


def test_snapshot_rejects_catastrophic_inventory_collapse(monkeypatch) -> None:
    monkeypatch.setenv("GAIA_STATIC_SNAPSHOT_MIN_ACTIVE_LISTINGS", "100")
    monkeypatch.setenv("GAIA_STATIC_SNAPSHOT_MIN_RETAINED_FRACTION", "0.5")
    with pytest.raises(RuntimeError, match="collapsed from 2101 to 400"):
        static_snapshot._validate_snapshot(
            _snapshot(400, 100, 20),
            _snapshot(2101, 457, 2),
        )


def test_rejected_snapshot_does_not_overwrite_last_known_good(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "snapshot.json"
    previous = _snapshot(2101, 457, 2)
    output.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setenv("GAIA_STATIC_SNAPSHOT_MIN_ACTIVE_LISTINGS", "100")
    monkeypatch.setattr(static_snapshot, "build_snapshot", lambda: _snapshot(19, 5, 29))

    with pytest.raises(RuntimeError, match="refusing degraded inventory snapshot"):
        static_snapshot.write_snapshot(output)

    assert json.loads(output.read_text(encoding="utf-8")) == previous
    assert not output.with_suffix(".json.tmp").exists()