from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .db import Database
from .db_base import iso


def _age_minutes(value: object, now: datetime) -> float | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return round(max(0.0, (now - parsed.astimezone(UTC)).total_seconds() / 60.0), 1)


def listing_freshness_state(database: Database, *, now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS active_families,
                COALESCE(SUM(direct_openings), 0) AS active_listings,
                COUNT(DISTINCT company) AS companies,
                MAX(latest_posted_at) AS newest_employer_posted_at,
                MAX(first_detected_at) AS newest_found_at,
                COUNT(*) FILTER (WHERE latest_posted_at >= now() - interval '24 hours') AS employer_posted_24h,
                COUNT(*) FILTER (WHERE latest_posted_at >= now() - interval '7 days') AS employer_posted_7d,
                COUNT(*) FILTER (WHERE first_detected_at >= now() - interval '24 hours') AS found_24h,
                COUNT(*) FILTER (WHERE first_detected_at >= now() - interval '7 days') AS found_7d
            FROM families
            WHERE target_match!='not_internship' AND direct_openings>0
            """
        ).fetchone()
    posted = row["newest_employer_posted_at"]
    found = row["newest_found_at"]
    candidates = [value for value in (posted, found) if value is not None]
    visible = max(candidates) if candidates else None
    return {
        "active_families": int(row["active_families"] or 0),
        "active_listings": int(row["active_listings"] or 0),
        "companies": int(row["companies"] or 0),
        "newest_employer_posted_at": iso(posted),
        "newest_employer_posted_age_hours": None if posted is None else round(float(_age_minutes(posted, current) or 0) / 60, 1),
        "newest_found_at": iso(found),
        "newest_found_age_hours": None if found is None else round(float(_age_minutes(found, current) or 0) / 60, 1),
        "newest_visible_activity_at": iso(visible),
        "newest_visible_activity_age_hours": None if visible is None else round(float(_age_minutes(visible, current) or 0) / 60, 1),
        "employer_posted_24h": int(row["employer_posted_24h"] or 0),
        "employer_posted_7d": int(row["employer_posted_7d"] or 0),
        "found_24h": int(row["found_24h"] or 0),
        "found_7d": int(row["found_7d"] or 0),
    }


def source_growth_state(database: Database, *, now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with database.connect() as connection:
        catalog = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE validated) AS validated,
                   COUNT(*) FILTER (WHERE first_discovered_at >= now() - interval '24 hours') AS new_24h,
                   COUNT(*) FILTER (WHERE first_discovered_at >= now() - interval '7 days') AS new_7d,
                   MAX(first_discovered_at) AS latest_unique_at,
                   MAX(last_discovered_at) AS latest_evidence_at
            FROM source_catalog
            """
        ).fetchone()
        candidates = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status='candidate') AS candidate,
                   COUNT(*) FILTER (WHERE status='retry') AS retry,
                   COUNT(*) FILTER (WHERE status='rejected') AS rejected,
                   COUNT(*) FILTER (WHERE status IN ('candidate','retry') AND next_probe_at<=now()
                     AND (lease_expires_at IS NULL OR lease_expires_at<now())) AS due,
                   COUNT(*) FILTER (WHERE first_seen_at >= now() - interval '24 hours') AS new_24h,
                   COUNT(*) FILTER (WHERE first_seen_at >= now() - interval '7 days') AS new_7d,
                   MAX(first_seen_at) AS latest_unique_at,
                   MAX(last_seen_at) AS latest_evidence_at,
                   MIN(next_probe_at) FILTER (WHERE status IN ('candidate','retry') AND next_probe_at<=now()
                     AND (lease_expires_at IS NULL OR lease_expires_at<now())) AS oldest_due_at
            FROM source_candidates
            """
        ).fetchone()
    unique_values = [value for value in (catalog["latest_unique_at"], candidates["latest_unique_at"]) if value is not None]
    evidence_values = [value for value in (catalog["latest_evidence_at"], candidates["latest_evidence_at"]) if value is not None]
    latest_unique = max(unique_values) if unique_values else None
    latest_evidence = max(evidence_values) if evidence_values else None
    return {
        "catalog_total": int(catalog["total"] or 0),
        "validated_total": int(catalog["validated"] or 0),
        "candidate_total": int(candidates["total"] or 0),
        "candidate": int(candidates["candidate"] or 0),
        "retry": int(candidates["retry"] or 0),
        "rejected": int(candidates["rejected"] or 0),
        "due": int(candidates["due"] or 0),
        "new_unique_24h": int(catalog["new_24h"] or 0) + int(candidates["new_24h"] or 0),
        "new_unique_7d": int(catalog["new_7d"] or 0) + int(candidates["new_7d"] or 0),
        "latest_unique_source_at": iso(latest_unique),
        "latest_unique_source_age_minutes": _age_minutes(latest_unique, current),
        "latest_source_evidence_at": iso(latest_evidence),
        "latest_source_evidence_age_minutes": _age_minutes(latest_evidence, current),
        "oldest_due_at": iso(candidates["oldest_due_at"]),
        "oldest_due_age_minutes": _age_minutes(candidates["oldest_due_at"], current),
    }


def stall_assessment(
    listing: dict[str, Any],
    growth: dict[str, Any],
    *,
    max_listing_hours: float = 48,
    max_candidate_due_minutes: float = 180,
) -> dict[str, Any]:
    failures: list[str] = []
    listing_age = listing.get("newest_visible_activity_age_hours")
    due_age = growth.get("oldest_due_age_minutes")
    if listing_age is None or float(listing_age) > max_listing_hours:
        failures.append("visible_listing_activity_stalled")
    if due_age is not None and float(due_age) > max_candidate_due_minutes:
        failures.append("source_candidate_backlog_stalled")
    return {"healthy": not failures, "failures": failures}
