from __future__ import annotations

from . import api as legacy

# v4 contract: confidence, posting recency, discovery recency, and verification
# recency are independent dimensions. The default `newest` sort is source-dated:
# employer dates first, undated roles last. `signal` is the explicit discovery-aware
# view for people who want GAIA's newest detections even when the employer publishes
# no date. `verified` remains recently checked employer evidence.
_POSTED_ACTIVITY_SQL = (
    "CASE "
    "WHEN latest_posted_at IS NULL THEN '-infinity'::timestamptz "
    "WHEN posted_precision='timestamp' THEN latest_posted_at "
    "ELSE date_trunc('day', latest_posted_at) END"
)
_FOUND_ACTIVITY_SQL = "date_trunc('hour', first_detected_at)"
_VISIBLE_ACTIVITY_SQL = (
    f"CASE WHEN latest_posted_at IS NULL "
    f"THEN {_FOUND_ACTIVITY_SQL} ELSE {_POSTED_ACTIVITY_SQL} END"
)
_VERIFIED_RANK_SQL = "CASE WHEN direct_openings > 0 THEN 0 ELSE 1 END"


def live_order_clause(sort: str) -> str:
    if sort == "company":
        return "lower(company), lower(title), family_key"
    if sort == "verified":
        return (
            f"{_VERIFIED_RANK_SQL}, "
            "last_verified_at DESC NULLS LAST, "
            f"{_POSTED_ACTIVITY_SQL} DESC, "
            f"{_FOUND_ACTIVITY_SQL} DESC, family_key"
        )
    if sort == "signal":
        return (
            f"{_VISIBLE_ACTIVITY_SQL} DESC, "
            f"{_VERIFIED_RANK_SQL}, "
            "last_verified_at DESC NULLS LAST, family_key"
        )

    # `newest`: real employer dates first; an undated role discovered five minutes
    # ago must not leapfrog a role the employer actually posted yesterday.
    return (
        f"{_POSTED_ACTIVITY_SQL} DESC, "
        "CASE posted_precision WHEN 'timestamp' THEN 0 WHEN 'day' THEN 1 ELSE 2 END, "
        f"{_FOUND_ACTIVITY_SQL} DESC, "
        f"{_VERIFIED_RANK_SQL}, "
        "last_verified_at DESC NULLS LAST, family_key"
    )


def install_feed_contract() -> None:
    """Install the v4 source-dated ordering contract for live family queries."""

    legacy._order_clause = live_order_clause
