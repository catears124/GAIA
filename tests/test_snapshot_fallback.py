from __future__ import annotations

import json

from starlette.requests import Request

from gaia.snapshot_fallback import snapshot_response


def _request(path: str, query: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 443),
        }
    )


def test_snapshot_families_preserves_useful_feed_contract() -> None:
    response = snapshot_response(
        _request("/api/families", b"track=tech&trust=verified&sort=newest&page=1&page_size=12")
    )
    assert response is not None
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["stale"] is True
    assert payload["total"] >= len(payload["items"]) > 0
    assert all(item["verified"] for item in payload["items"])
    assert all(item["openings"] for item in payload["items"])


def test_snapshot_stats_matches_visible_verified_inventory_units() -> None:
    response = snapshot_response(_request("/api/stats"))
    assert response is not None
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["stale"] is True
    assert payload["active_listings"] >= payload["role_families"] > 0
    assert payload["companies"] > 0
    assert payload["activity_units"]["new_today"] == "role_family"


def test_snapshot_families_supports_search_and_company_filters() -> None:
    response = snapshot_response(_request("/api/families", b"q=quantitative&company=alpha"))
    assert response is not None
    payload = json.loads(response.body)
    assert payload["items"]
    assert all("alpha" in item["company"].casefold() for item in payload["items"])
    assert all("quantitative" in f"{item['company']} {item['title']}".casefold() for item in payload["items"])
