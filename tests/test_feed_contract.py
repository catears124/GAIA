from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gaia.feed_contract import live_order_clause
from gaia.snapshot_fallback import _stats
from gaia.v4_snapshot import sort_families

FRONTEND = Path(__file__).parents[1] / "src" / "gaia" / "frontend"


def _family(
    key: str,
    *,
    verified: bool,
    event: datetime,
    posted: datetime | None = None,
    sensor_reported: datetime | None = None,
    found: datetime | None = None,
    checked: datetime | None = None,
) -> dict[str, object]:
    found = found or event
    return {
        "family_key": key,
        "title": key,
        "company": "Example",
        "category": "software",
        "target_match": "exact",
        "locations": [],
        "verified": verified,
        "direct_openings": 1 if verified else 0,
        "backstop_openings": 0 if verified else 1,
        "market_event_at": event.isoformat(),
        "latest_posted_at": posted.isoformat() if posted else None,
        "latest_sensor_reported_at": sensor_reported.isoformat() if sensor_reported else None,
        "market_first_seen_at": found.isoformat(),
        "first_detected_at": found.isoformat(),
        "last_verified_at": checked.isoformat() if checked else None,
    }


def test_live_newest_feed_orders_by_market_activity_before_confidence() -> None:
    clause = live_order_clause("newest")
    assert clause.startswith("CASE WHEN latest_posted_at IS NULL")
    assert "CASE WHEN direct_openings > 0 THEN 0 ELSE 1 END" in clause
    assert clause.index("CASE WHEN direct_openings > 0 THEN 0 ELSE 1 END") > clause.index("DESC")


def test_snapshot_newest_feed_does_not_bury_fresh_lead() -> None:
    now = datetime.now(UTC)
    verified = _family(
        "verified",
        verified=True,
        event=now - timedelta(days=5),
        posted=now - timedelta(days=5),
        checked=now - timedelta(hours=1),
    )
    fresh_lead = _family(
        "fresh-lead",
        verified=False,
        event=now - timedelta(minutes=5),
        sensor_reported=now - timedelta(minutes=5),
    )
    items = [verified, fresh_lead]
    sort_families(items, "newest")
    assert [item["family_key"] for item in items] == ["fresh-lead", "verified"]


def test_verification_time_does_not_make_old_role_new() -> None:
    now = datetime.now(UTC)
    old_recently_checked = _family(
        "old",
        verified=True,
        event=now - timedelta(days=8),
        posted=now - timedelta(days=8),
        checked=now,
    )
    recent = _family(
        "recent",
        verified=True,
        event=now - timedelta(hours=3),
        posted=now - timedelta(hours=3),
        checked=now - timedelta(days=1),
    )
    items = [old_recently_checked, recent]
    sort_families(items, "newest")
    assert [item["family_key"] for item in items] == ["recent", "old"]


def test_snapshot_stats_separate_verified_freshness_from_market_signals() -> None:
    now = datetime.now(UTC)
    snapshot = {
        "generated_at": now.isoformat(),
        "source_activity_at": now.isoformat(),
        "family_index": [
            _family(
                "fresh-verified",
                verified=True,
                event=now - timedelta(hours=3),
                posted=now - timedelta(hours=3),
                checked=now,
            ),
            _family(
                "fresh-lead",
                verified=False,
                event=now - timedelta(hours=1),
                sensor_reported=now - timedelta(hours=1),
            ),
            _family(
                "old-verified",
                verified=True,
                event=now - timedelta(days=4),
                posted=now - timedelta(days=4),
                checked=now,
            ),
        ],
    }
    response = _stats(snapshot)
    payload = json.loads(response.body)
    assert payload["new_today"] == 1
    assert payload["new_verified_24h"] == 1
    assert payload["market_events_24h"] == 2
    assert payload["dated_market_events_24h"] == 2


def test_frontend_defaults_to_market_view_and_stale_fallback_is_market_first() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    contract = (FRONTEND / "feed-contract.js").read_text(encoding="utf-8")
    assert '<option value="all" selected>Verified + leads</option>' in html
    assert '<option value="" selected>All active cycles</option>' in html
    assert 'feed-contract.js?v=1.0.0' in html
    assert 'itemActivity(right) - itemActivity(left)' in contract
    assert 'Number(isVerified(right)) - Number(isVerified(left))' in contract
    assert contract.index('itemActivity(right) - itemActivity(left)') < contract.index('Number(isVerified(right)) - Number(isVerified(left))')
    assert 'X-GAIA-Feed-Contract", "market-first-v4"' in contract
    assert 'trust.value = "verified"' not in contract
    assert '["/api/families", "/api/stats"].includes(url.pathname)' in contract
