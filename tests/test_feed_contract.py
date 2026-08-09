from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gaia.feed_contract import live_order_clause
from gaia.snapshot_fallback import _sort_items, _stats

FRONTEND = Path(__file__).parents[1] / "src" / "gaia" / "frontend"


def _family(
    key: str,
    *,
    verified: bool,
    posted: datetime | None,
    found: datetime,
    checked: datetime,
) -> dict[str, object]:
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
        "latest_posted_at": posted.isoformat() if posted else None,
        "first_detected_at": found.isoformat(),
        "last_verified_at": checked.isoformat(),
    }


def test_live_newest_feed_ranks_direct_applications_before_leads() -> None:
    clause = live_order_clause("newest")
    assert clause.startswith("CASE WHEN direct_openings > 0 THEN 0 ELSE 1 END")
    assert "CASE WHEN latest_posted_at IS NULL THEN 1 ELSE 0 END" in clause


def test_snapshot_all_feed_cannot_be_flooded_by_fresh_leads() -> None:
    now = datetime.now(UTC)
    verified = _family(
        "verified",
        verified=True,
        posted=now - timedelta(days=5),
        found=now - timedelta(days=5),
        checked=now - timedelta(hours=1),
    )
    fresh_lead = _family(
        "fresh-lead",
        verified=False,
        posted=None,
        found=now - timedelta(minutes=5),
        checked=now - timedelta(minutes=2),
    )
    items = [fresh_lead, verified]
    _sort_items(items, "newest")
    assert [item["family_key"] for item in items] == ["verified", "fresh-lead"]


def test_dated_verified_jobs_rank_before_undated_verified_jobs() -> None:
    now = datetime.now(UTC)
    dated = _family(
        "dated",
        verified=True,
        posted=now - timedelta(days=8),
        found=now - timedelta(days=8),
        checked=now,
    )
    undated = _family(
        "undated",
        verified=True,
        posted=None,
        found=now - timedelta(minutes=1),
        checked=now,
    )
    items = [undated, dated]
    _sort_items(items, "newest")
    assert [item["family_key"] for item in items] == ["dated", "undated"]


def test_snapshot_stats_count_new_verified_families_instead_of_returning_zero() -> None:
    now = datetime.now(UTC)
    snapshot = {
        "generated_at": now.isoformat(),
        "source_activity_at": now.isoformat(),
        "family_index": [
            _family(
                "fresh-verified",
                verified=True,
                posted=now - timedelta(hours=3),
                found=now - timedelta(hours=2),
                checked=now,
            ),
            _family(
                "fresh-lead",
                verified=False,
                posted=None,
                found=now - timedelta(hours=1),
                checked=now,
            ),
        ],
    }
    response = _stats(snapshot)
    payload = json.loads(response.body)
    assert payload["new_today"] == 1
    assert payload["new_24h"] == 1
    assert payload["new_families_24h"] == 1


def test_frontend_defaults_to_verified_and_recomputes_stale_feed() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    contract = (FRONTEND / "feed-contract.js").read_text(encoding="utf-8")
    assert '<option value="verified" selected>Employer verified</option>' in html
    assert 'feed-contract.js?v=1.0.0' in html
    assert 'Number(isVerified(right)) - Number(isVerified(left))' in contract
    assert 'url.pathname === "/api/families"' not in contract  # exact list guards both families + stats
    assert '["/api/families", "/api/stats"].includes(url.pathname)' in contract
    assert "newFamilies" in contract
    assert 'window.addEventListener("gaia:stale-data"' in contract
