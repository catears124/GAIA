from __future__ import annotations

from . import api as legacy

# v4 contract: confidence and recency are independent dimensions.
#
# "newest" answers one question only: what changed in the internship market most
# recently? Employer verification is a tiebreaker, never a gate that can bury a
# newly detected role below week-old inventory. The explicit "verified" sort still
# exists for users who want recently checked employer applications.
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
            f"{_VISIBLE_ACTIVITY_SQL} DESC, "
            "family_key"
        )

    return (
        f"{_VISIBLE_ACTIVITY_SQL} DESC, "
        f"{_VERIFIED_RANK_SQL}, "
        "CASE posted_precision WHEN 'timestamp' THEN 0 WHEN 'day' THEN 1 ELSE 2 END, "
        "last_verified_at DESC NULLS LAST, family_key"
    )


def install_feed_contract() -> None:
    """Install the v4 market-first ordering contract for live family queries."""

    legacy._order_clause = live_order_clause
