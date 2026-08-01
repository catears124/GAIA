from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .db import Database
from .db_base import iso
from .universe import universe_summary

BAD_STATUSES = ("broken", "blocked", "truncated", "partial")


FRESHNESS_FLOOR_SECONDS = 90 * 60
FRESHNESS_INTERVAL_MULTIPLIER = 3
ACTIVE_CATCHUP_GRACE_MINUTES = 30
DEFAULT_MAX_LISTING_ACTIVITY_HOURS = 48
DEFAULT_MAX_SOURCE_DISCOVERY_HOURS = 36
DEFAULT_MAX_CANDIDATE_DUE_MINUTES = 180


def _utc_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_minutes(value: object, *, now: datetime) -> float | None:
    parsed = _utc_datetime(value)
    if parsed is None:
        return None
    return round(max(0.0, (now - parsed).total_seconds() / 60.0), 1)


def inventory_state(database: Database) -> dict[str, Any]:
    """Return mutually exclusive source-health counts for the public inventory."""
    with database.connect() as connection:
        row = connection.execute(
            """
            WITH current_targets AS (
                SELECT
                    target.*,
                    GREATEST(target.interval_seconds * %s, %s) AS freshness_seconds
                FROM crawl_targets AS target
                JOIN source_catalog AS catalog USING(source)
                WHERE target.enabled
                  AND target.scheduled
                  AND catalog.validated
                  AND catalog.scope='current'
            )
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE target.lease_expires_at > now()
                ) AS running,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NULL
                ) AS never_completed,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NOT NULL
                      AND target.last_complete_at <
                          now() - make_interval(secs => target.freshness_seconds)
                ) AS overdue,
                COUNT(*) FILTER (
                    WHERE target.last_status = ANY(%s)
                ) AS degraded,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NOT NULL
                      AND target.last_complete_at >=
                          now() - make_interval(secs => target.freshness_seconds)
                      AND target.last_status <> ALL(%s)
                ) AS fresh,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NULL
                       OR target.last_complete_at <
                          now() - make_interval(secs => target.freshness_seconds)
                       OR target.last_status = ANY(%s)
                ) AS unhealthy,
                MAX(target.last_finished_at) AS latest_activity_at,
                MIN(target.last_complete_at) FILTER (
                    WHERE target.last_complete_at IS NOT NULL
                ) AS coverage_watermark
            FROM current_targets AS target
            """,
            (
                FRESHNESS_INTERVAL_MULTIPLIER,
                FRESHNESS_FLOOR_SECONDS,
                list(BAD_STATUSES),
                list(BAD_STATUSES),
                list(BAD_STATUSES),
            ),
        ).fetchone()
        historical = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM crawl_targets AS target
            JOIN source_catalog AS catalog USING(source)
            WHERE target.scheduled
              AND catalog.validated
              AND catalog.scope='historical'
            """
        ).fetchone()

    state: dict[str, Any] = {key: row[key] for key in row.keys()}
    for key in (
        "total",
        "running",
        "never_completed",
        "overdue",
        "degraded",
        "fresh",
        "unhealthy",
    ):
        state[key] = int(state.get(key) or 0)
    state["historical"] = int(historical["count"] or 0)
    state["latest_activity_at"] = iso(state.get("latest_activity_at"))
    state["coverage_watermark"] = iso(state.get("coverage_watermark"))
    state["freshness_floor_seconds"] = FRESHNESS_FLOOR_SECONDS
    total = int(state["total"])
    state["fresh_percent"] = round(100 * int(state["fresh"]) / total, 1) if total else 0.0
    state["healthy"] = bool(total) and int(state["unhealthy"]) == 0
    return state


def source_growth_state(
    database: Database,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Expose whether the employer/source graph is actually expanding and draining."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with database.connect() as connection:
        catalog = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE validated) AS validated,
                COUNT(*) FILTER (WHERE validated AND scope='current') AS current_validated,
                COUNT(*) FILTER (WHERE validated AND scope='historical') AS historical_validated,
                COUNT(*) FILTER (
                    WHERE first_discovered_at >= now() - interval '24 hours'
                ) AS new_24h,
                COUNT(*) FILTER (
                    WHERE first_discovered_at >= now() - interval '7 days'
                ) AS new_7d,
                MAX(first_discovered_at) AS latest_unique_at,
                MAX(last_discovered_at) AS latest_evidence_at
            FROM source_catalog
            """
        ).fetchone()
        candidates = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status='candidate') AS candidate,
                COUNT(*) FILTER (WHERE status='retry') AS retry,
                COUNT(*) FILTER (WHERE status='rejected') AS rejected,
                COUNT(*) FILTER (
                    WHERE status IN ('candidate','retry')
                      AND next_probe_at<=now()
                      AND (lease_expires_at IS NULL OR lease_expires_at<now())
                ) AS due,
                COUNT(*) FILTER (WHERE lease_expires_at>now()) AS leased,
                COUNT(*) FILTER (
                    WHERE first_seen_at >= now() - interval '24 hours'
                ) AS new_24h,
                COUNT(*) FILTER (
                    WHERE first_seen_at >= now() - interval '7 days'
                ) AS new_7d,
                MAX(first_seen_at) AS latest_unique_at,
                MAX(last_seen_at) AS latest_evidence_at,
                MIN(next_probe_at) FILTER (
                    WHERE status IN ('candidate','retry')
                      AND next_probe_at<=now()
                      AND (lease_expires_at IS NULL OR lease_expires_at<now())
                ) AS oldest_due_at
            FROM source_candidates
            """
        ).fetchone()
        catalog_kinds = connection.execute(
            """
            SELECT kind, COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE validated) AS validated
            FROM source_catalog
            GROUP BY kind
            ORDER BY kind
            """
        ).fetchall()
        candidate_kinds = connection.execute(
            """
            SELECT kind, COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status IN ('candidate','retry')) AS actionable
            FROM source_candidates
            GROUP BY kind
            ORDER BY kind
            """
        ).fetchall()

    catalog_latest = _utc_datetime(catalog["latest_unique_at"])
    candidate_latest = _utc_datetime(candidates["latest_unique_at"])
    latest_unique = max(
        (value for value in (catalog_latest, candidate_latest) if value is not None),
        default=None,
    )
    catalog_evidence = _utc_datetime(catalog["latest_evidence_at"])
    candidate_evidence = _utc_datetime(candidates["latest_evidence_at"])
    latest_evidence = max(
        (value for value in (catalog_evidence, candidate_evidence) if value is not None),
        default=None,
    )

    return {
        "catalog_total": int(catalog["total"] or 0),
        "validated_total": int(catalog["validated"] or 0),
        "current_validated": int(catalog["current_validated"] or 0),
        "historical_validated": int(catalog["historical_validated"] or 0),
        "candidate_total": int(candidates["total"] or 0),
        "candidate": int(candidates["candidate"] or 0),
        "retry": int(candidates["retry"] or 0),
        "rejected": int(candidates["rejected"] or 0),
        "due": int(candidates["due"] or 0),
        "leased": int(candidates["leased"] or 0),
        "new_unique_24h": int(catalog["new_24h"] or 0) + int(candidates["new_24h"] or 0),
        "new_unique_7d": int(catalog["new_7d"] or 0) + int(candidates["new_7d"] or 0),
        "latest_unique_source_at": iso(latest_unique),
        "latest_unique_source_age_minutes": _age_minutes(latest_unique, now=current),
        "latest_source_evidence_at": iso(latest_evidence),
        "latest_source_evidence_age_minutes": _age_minutes(latest_evidence, now=current),
        "oldest_due_at": iso(candidates["oldest_due_at"]),
        "oldest_due_age_minutes": _age_minutes(candidates["oldest_due_at"], now=current),
        "catalog_by_kind": {
            str(row["kind"]): {
                "total": int(row["total"] or 0),
                "validated": int(row["validated"] or 0),
            }
            for row in catalog_kinds
        },
        "candidates_by_kind": {
            str(row["kind"]): {
                "total": int(row["total"] or 0),
                "actionable": int(row["actionable"] or 0),
            }
            for row in candidate_kinds
        },
    }


def listing_freshness_state(
    database: Database,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Measure actual visible-job advancement, not merely successful crawler traffic."""
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
                COUNT(*) FILTER (
                    WHERE latest_posted_at >= now() - interval '24 hours'
                ) AS employer_posted_24h,
                COUNT(*) FILTER (
                    WHERE latest_posted_at >= now() - interval '7 days'
                ) AS employer_posted_7d,
                COUNT(*) FILTER (
                    WHERE first_detected_at >= now() - interval '24 hours'
                ) AS found_24h,
                COUNT(*) FILTER (
                    WHERE first_detected_at >= now() - interval '7 days'
                ) AS found_7d
            FROM families
            WHERE target_match!='not_internship'
              AND direct_openings>0
            """
        ).fetchone()

    newest_posted = _utc_datetime(row["newest_employer_posted_at"])
    newest_found = _utc_datetime(row["newest_found_at"])
    newest_activity = max(
        (value for value in (newest_posted, newest_found) if value is not None),
        default=None,
    )
    return {
        "active_families": int(row["active_families"] or 0),
        "active_listings": int(row["active_listings"] or 0),
        "companies": int(row["companies"] or 0),
        "newest_employer_posted_at": iso(newest_posted),
        "newest_employer_posted_age_hours": (
            round(float(_age_minutes(newest_posted, now=current) or 0.0) / 60.0, 1)
            if newest_posted is not None
            else None
        ),
        "newest_found_at": iso(newest_found),
        "newest_found_age_hours": (
            round(float(_age_minutes(newest_found, now=current) or 0.0) / 60.0, 1)
            if newest_found is not None
            else None
        ),
        "newest_visible_activity_at": iso(newest_activity),
        "newest_visible_activity_age_hours": (
            round(float(_age_minutes(newest_activity, now=current) or 0.0) / 60.0, 1)
            if newest_activity is not None
            else None
        ),
        "employer_posted_24h": int(row["employer_posted_24h"] or 0),
        "employer_posted_7d": int(row["employer_posted_7d"] or 0),
        "found_24h": int(row["found_24h"] or 0),
        "found_7d": int(row["found_7d"] or 0),
    }


def production_report(
    database: Database,
    *,
    max_activity_minutes: int = 90,
    min_sources: int = 100,
    min_active_listings: int = 25,
    require_healthy: bool = False,
    require_universe: bool = False,
    max_listing_activity_hours: int = DEFAULT_MAX_LISTING_ACTIVITY_HOURS,
    max_source_discovery_hours: int = DEFAULT_MAX_SOURCE_DISCOVERY_HOURS,
    max_candidate_due_minutes: int = DEFAULT_MAX_CANDIDATE_DUE_MINUTES,
) -> dict[str, Any]:
    """Evaluate whether hosted inventory is alive, populated, advancing, and expanding."""
    now = datetime.now(UTC)
    state = inventory_state(database)
    growth = source_growth_state(database, now=now)
    listings = listing_freshness_state(database, now=now)
    census = universe_summary(database, limit=1)
    with database.connect() as connection:
        snapshots = connection.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE finished_at >= now() - interval '30 minutes'
                ) AS recent_snapshots,
                COUNT(*) FILTER (
                    WHERE finished_at >= now() - interval '30 minutes'
                      AND status IN ('broken','blocked','truncated','partial')
                ) AS recent_failed_snapshots,
                MAX(finished_at) AS latest_snapshot_at
            FROM source_snapshots
            """
        ).fetchone()

    latest_raw = state.get("latest_activity_at")
    latest = datetime.fromisoformat(str(latest_raw)) if latest_raw else None
    activity_age_minutes = (
        max(0.0, (now - latest).total_seconds() / 60.0) if latest is not None else None
    )
    active_catchup = bool(
        int(state["running"])
        and activity_age_minutes is not None
        and activity_age_minutes <= min(max_activity_minutes, ACTIVE_CATCHUP_GRACE_MINUTES)
    )
    state["active_catchup"] = active_catchup

    errors: list[str] = []
    warnings: list[str] = []
    total = int(state["total"])
    active_listings = int(listings["active_listings"])
    census_summary = dict(census.get("summary") or {})
    unresolved_employers = int(census_summary.get("unresolved_employers") or 0)
    if total < min_sources:
        errors.append(f"validated source count {total} is below floor {min_sources}")
    if active_listings < min_active_listings:
        errors.append(
            f"active direct listing count {active_listings} is below floor {min_active_listings}"
        )
    if activity_age_minutes is None:
        errors.append("inventory has no completed source activity")
    elif activity_age_minutes > max_activity_minutes:
        errors.append(
            f"inventory activity is {activity_age_minutes:.1f} minutes old; "
            f"maximum is {max_activity_minutes}"
        )
    listing_activity_age = listings["newest_visible_activity_age_hours"]
    if listing_activity_age is None:
        errors.append("visible inventory has no employer-posted or GAIA-found activity")
    elif float(listing_activity_age) > max_listing_activity_hours:
        errors.append(
            f"newest visible listing activity is {listing_activity_age} hours old; "
            f"maximum is {max_listing_activity_hours}"
        )
    if int(state["fresh"]) == 0:
        if active_catchup:
            warnings.append(
                f"inventory has no fresh completed sources yet, but {state['running']} "
                "workers are actively catching up"
            )
        else:
            errors.append("no validated current source is fresh")
    if require_healthy and not bool(state["healthy"]):
        errors.append(f"{state['unhealthy']} sources are not current")
    elif not bool(state["healthy"]):
        warnings.append(
            f"inventory is catching up: {state['fresh']}/{state['total']} sources are fresh"
        )
    if int(state["degraded"]):
        warnings.append(f"{state['degraded']} sources have a degraded latest result")

    oldest_due_age = growth["oldest_due_age_minutes"]
    if oldest_due_age is not None and float(oldest_due_age) > max_candidate_due_minutes:
        errors.append(
            f"oldest due source candidate is {oldest_due_age} minutes overdue; "
            f"maximum is {max_candidate_due_minutes}"
        )
    elif int(growth["due"]):
        warnings.append(f"{growth['due']} source candidates are waiting for validation")

    latest_unique_age = growth["latest_unique_source_age_minutes"]
    if unresolved_employers > 0:
        if latest_unique_age is None:
            errors.append(
                f"{unresolved_employers} employers are unresolved and no source has ever been discovered"
            )
        elif float(latest_unique_age) > max_source_discovery_hours * 60:
            errors.append(
                f"source graph has not added a unique source for {latest_unique_age} minutes "
                f"while {unresolved_employers} employers remain unresolved; "
                f"maximum is {max_source_discovery_hours * 60}"
            )
        elif int(growth["new_unique_24h"]) == 0:
            warnings.append(
                f"source graph added no unique source in 24 hours while "
                f"{unresolved_employers} employers remain unresolved"
            )

    if require_universe:
        if not bool(census.get("ready")):
            errors.append("employer universe read model is missing")
        elif int(census_summary.get("known_employers") or 0) == 0:
            errors.append("employer universe contains no employers")
        elif int(census_summary.get("enumerated_employers") or 0) == 0:
            errors.append("employer universe contains no independently enumerated employers")
    elif not bool(census.get("ready")):
        warnings.append("employer universe read model has not been built")

    return {
        "ok": not errors,
        "checked_at": now.isoformat(),
        "activity_age_minutes": (
            round(activity_age_minutes, 1) if activity_age_minutes is not None else None
        ),
        "limits": {
            "max_activity_minutes": max_activity_minutes,
            "min_sources": min_sources,
            "min_active_listings": min_active_listings,
            "require_healthy": require_healthy,
            "require_universe": require_universe,
            "max_listing_activity_hours": max_listing_activity_hours,
            "max_source_discovery_hours": max_source_discovery_hours,
            "max_candidate_due_minutes": max_candidate_due_minutes,
        },
        "inventory": state,
        "product": {
            "active_families": int(listings["active_families"]),
            "active_listings": active_listings,
            "companies": int(listings["companies"]),
        },
        "listing_freshness": listings,
        "source_growth": growth,
        "universe": census,
        "snapshots": {
            "recent_30m": int(snapshots["recent_snapshots"] or 0),
            "recent_failed_30m": int(snapshots["recent_failed_snapshots"] or 0),
            "latest_at": iso(snapshots["latest_snapshot_at"]),
        },
        "errors": errors,
        "warnings": warnings,
    }
