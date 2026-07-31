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
