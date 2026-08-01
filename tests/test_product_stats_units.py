from __future__ import annotations

import inspect

from gaia.product_api import _activity_stats, _live_order_clause, live_families


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


def test_live_feed_defaults_to_confirmed_target_internships() -> None:
    target = inspect.signature(live_families).parameters["target"]

    assert target.default == "default"


def test_newest_order_uses_employer_date_before_recovery_time() -> None:
    order = _live_order_clause("newest")
    employer_branch = order.index("CASE WHEN latest_posted_at IS NULL")
    detection_fallback = order.index("date_trunc('hour', first_detected_at)")

    assert "GREATEST" not in order
    assert employer_branch < detection_fallback
