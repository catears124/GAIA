from __future__ import annotations

import inspect

from gaia import api as legacy
from gaia.product_api import _activity_stats, _live_order_clause, live_families, live_stats


def test_visible_new_today_is_source_dated_and_discovery_is_separate() -> None:
    payload = _activity_stats(
        {"new_families_today": 7, "discovered_families_today": 19},
        {"new_urls_today": 29, "removed_urls_today": 4},
    )

    assert payload["new_today"] == 7
    assert payload["new_24h"] == 7
    assert payload["new_verified_24h"] == 7
    assert payload["new_families_24h"] == 7
    assert payload["discovered_24h"] == 19
    assert payload["new_urls_24h"] == 29
    assert payload["removed_urls_24h"] == 4
    assert payload["net_urls_24h"] == 25
    assert payload["activity_units"] == {
        "new_today": "verified_role_family_with_employer_posted_timestamp_in_24h",
        "discovered_24h": "verified_role_family_first_seen_by_gaia_in_24h",
        "url_movement": "canonical_apply_url",
    }


def test_live_feed_defaults_to_all_active_cycles() -> None:
    target = inspect.signature(live_families).parameters["target"]
    assert target.default == ""

    params: list[object] = []
    assert legacy._target_clause("", params) == "TRUE"
    assert params == []


def test_live_stats_cover_current_and_unknown_cycles_not_only_2027() -> None:
    source = inspect.getsource(live_stats)
    assert "year=2027" not in source
    assert "year IS NULL OR year >= EXTRACT(YEAR FROM now())::int" in source


def test_live_summer_filter_uses_year_and_season_not_classifier_label() -> None:
    params: list[object] = []
    clause = legacy._target_clause("exact", params)
    assert "year=%s" in clause
    assert "season" in clause
    assert "target_match" not in clause
    assert params == [2027, "summer"]


def test_live_posted_within_never_falls_back_to_first_detected() -> None:
    source = inspect.getsource(legacy._list_families)
    posted_block = source[source.index("if posted_within:") : source.index("order_params")]
    assert "latest_posted_at" in posted_block
    assert "first_detected_at" not in posted_block


def test_newest_order_uses_employer_date_before_recovery_time() -> None:
    order = _live_order_clause("newest")
    employer_branch = order.index("CASE WHEN latest_posted_at IS NULL")
    detection_fallback = order.index("date_trunc('hour', first_detected_at)")

    assert "GREATEST" not in order
    assert employer_branch < detection_fallback
