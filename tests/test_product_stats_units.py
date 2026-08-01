from __future__ import annotations

from gaia.product_api import _activity_stats


def test_visible_found_today_counts_role_families_not_raw_urls() -> None:
    payload = _activity_stats(
        {"new_families_today": 19},
        {"new_urls_today": 29, "removed_urls_today": 4},
    )

    assert payload["new_today"] == 19
    assert payload["new_24h"] == 19
    assert payload["new_families_24h"] == 19
    assert payload["new_urls_24h"] == 29
    assert payload["removed_urls_24h"] == 4
    assert payload["net_urls_24h"] == 25
    assert payload["activity_units"] == {
        "new_today": "role_family",
        "url_movement": "canonical_apply_url",
    }
