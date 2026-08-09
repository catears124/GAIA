from __future__ import annotations

from . import api as legacy

# The public feed is an application feed, not a crawler-event log. Employer-verified
# applications must remain ahead of unverified market leads even when a lead was just
# discovered. Within each confidence tier, a real employer posting date is stronger
# recency evidence than GAIA discovery time; discovery is only a fallback for undated
# roles.
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
_DATED_RANK_SQL = "CASE WHEN latest_posted_at IS NULL THEN 1 ELSE 0 END"


def live_order_clause(sort: str) -> str:
    if sort == "company":
        return "lower(company), lower(title), family_key"
    if sort == "verified":
        return (
            f"{_VERIFIED_RANK_SQL}, "
            "last_verified_at DESC, "
            f"{_DATED_RANK_SQL}, "
            f"{_VISIBLE_ACTIVITY_SQL} DESC, "
            "family_key"
        )

    return (
        f"{_VERIFIED_RANK_SQL}, "
        f"{_DATED_RANK_SQL}, "
        f"{_VISIBLE_ACTIVITY_SQL} DESC, "
        "CASE posted_precision WHEN 'timestamp' THEN 0 WHEN 'day' THEN 1 ELSE 2 END, "
        f"{_POSTED_ACTIVITY_SQL} DESC, "
        f"{_FOUND_ACTIVITY_SQL} DESC, "
        "last_verified_at DESC, family_key"
    )


def install_feed_contract() -> None:
    """Install one feed-ordering contract for every live family query."""

    legacy._order_clause = live_order_clause
