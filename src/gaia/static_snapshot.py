from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import psycopg

from . import api as legacy
from .db_base import iso
from .health import BAD_STATUSES, FRESHNESS_FLOOR_SECONDS, FRESHNESS_INTERVAL_MULTIPLIER
from .product_api import live_facets, live_families, live_health

DEFAULT_OUTPUT = Path(__file__).with_name("frontend") / "last-known-inventory.json"
FAMILY_FIELDS = (
    "family_key",
    "title",
    "company",
    "category",
    "target_match",
    "year",
    "season",
    "locations",
    "opening_count",
    "direct_openings",
    "backstop_openings",
    "verified",
    "quality",
    "latest_posted_at",
    "posted_precision",
    "first_detected_at",
    "last_verified_at",
    "remote",
)
OPENING_FIELDS = (
    "apply_url",
    "source",
    "source_mode",
    "location",
    "posted_at",
    "first_detected_at",
)


def _key(path: str, **params: object) -> str:
    values = [
        (name, str(value).lower() if isinstance(value, bool) else str(value))
        for name, value in params.items()
        if value not in (None, "", False, 0)
    ]
    values.sort()
    query = urlencode(values)
    return f"{path}?{query}" if query else path


def _families(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "q": "",
        "category": "",
        "target": "",
        "track": "tech",
        "trust": "all",
        "location": "",
        "sort": "newest",
        "page": 1,
        "page_size": 48,
        "company": "",
        "remote": False,
        "posted_within": 0,
    }
    values.update(overrides)
    return live_families(**values)  # type: ignore[arg-type]


def _compact_family(raw: dict[str, object]) -> dict[str, object]:
    compact = {name: raw[name] for name in FAMILY_FIELDS if name in raw}
    openings: list[dict[str, object]] = []
    for opening in raw.get("openings") or []:
        if isinstance(opening, dict):
            openings.append({name: opening[name] for name in OPENING_FIELDS if name in opening})
    compact["openings"] = openings
    return compact


def _family_index() -> tuple[list[dict[str, object]], int, bool]:
    """Legacy/test path: page the public family API until all visible rows are exported."""

    page_size = 100
    max_pages = max(1, int(os.getenv("GAIA_STATIC_SNAPSHOT_MAX_PAGES", "100")))
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    expected_total = 0

    for page in range(1, max_pages + 1):
        payload = _families(page=page, page_size=page_size)
        expected_total = max(expected_total, int(payload.get("total") or 0))
        rows = payload.get("items") or []
        if not isinstance(rows, list):
            rows = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("family_key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(_compact_family(raw))
        if not rows or len(items) >= expected_total:
            break

    complete = expected_total == 0 or len(items) >= expected_total
    return items, expected_total, complete


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _family_activity(item: dict[str, object]) -> float:
    """Return the same visible recency used by the live product API.

    An employer-supplied posting date is authoritative when present. Discovery time
    is only a fallback for jobs whose employer did not publish a date; discovering an
    old posting today must never make it rank as a newly posted role.
    """

    posted = _parse_timestamp(item.get("latest_posted_at"))
    if posted is not None:
        return posted.timestamp()
    found = _parse_timestamp(item.get("first_detected_at"))
    return found.timestamp() if found else 0.0


def _verified_activity(item: dict[str, object]) -> float:
    value = _parse_timestamp(item.get("last_verified_at"))
    return value.timestamp() if value else 0.0


def _matches_target(item: dict[str, object], target: str) -> bool:
    if not target:
        return True
    match = str(item.get("target_match") or "")
    if target == "default":
        return match in legacy.TARGET_MATCHES
    return match == target


def _filter_family_index(
    index: list[dict[str, object]],
    *,
    q: str = "",
    category: str = "",
    target: str = "",
    trust: str = "all",
    company: str = "",
    location: str = "",
    remote: bool = False,
    posted_within: int = 0,
) -> list[dict[str, object]]:
    tokens = [token.casefold() for token in q.split() if token]
    location_query = location.strip().casefold()
    company_query = company.strip().casefold()
    cutoff = datetime.now(UTC) - timedelta(days=max(0, posted_within)) if posted_within else None
    result: list[dict[str, object]] = []
    for item in index:
        locations = [str(value) for value in item.get("locations") or []]
        location_text = " ".join(locations).casefold()
        haystack = f"{item.get('title') or ''} {item.get('company') or ''} {location_text}".casefold()
        if any(token not in haystack for token in tokens):
            continue
        if category and str(item.get("category") or "") != category:
            continue
        if not _matches_target(item, target):
            continue
        verified = bool(item.get("verified"))
        if trust == "verified" and not verified:
            continue
        if trust == "leads" and verified:
            continue
        if company_query and str(item.get("company") or "").casefold() != company_query:
            continue
        if location_query and location_query not in location_text:
            continue
        if remote and not (bool(item.get("remote")) or "remote" in location_text):
            continue
        if cutoff:
            activity = _parse_timestamp(item.get("latest_posted_at")) or _parse_timestamp(
                item.get("first_detected_at")
            )
            if activity is None or activity < cutoff:
                continue
        result.append(item)
    return result


def _family_page_from_index(
    index: list[dict[str, object]],
    *,
    page: int = 1,
    page_size: int = 48,
    sort: str = "newest",
    **filters: object,
) -> dict[str, object]:
    items = _filter_family_index(index, **filters)  # type: ignore[arg-type]
    if sort == "company":
        items.sort(
            key=lambda item: (
                str(item.get("company") or "").casefold(),
                str(item.get("title") or "").casefold(),
                str(item.get("family_key") or ""),
            )
        )
    elif sort == "verified":
        items.sort(key=lambda item: (_verified_activity(item), _family_activity(item)), reverse=True)
    else:
        items.sort(key=lambda item: (_family_activity(item), _verified_activity(item)), reverse=True)
    start = max(0, page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "total": len(items),
        "page": page,
        "page_size": page_size,
        "offline": True,
    }


def _facets_from_index(
    index: list[dict[str, object]], *, trust: str = "all", target: str = ""
) -> dict[str, object]:
    items = _filter_family_index(index, trust=trust, target=target)
    companies = Counter(str(item.get("company") or "") for item in items if item.get("company"))
    categories = Counter(str(item.get("category") or "") for item in items if item.get("category"))
    remote_count = sum(
        1
        for item in items
        if bool(item.get("remote"))
        or "remote" in " ".join(str(value) for value in item.get("locations") or []).casefold()
    )

    def ranked(counter: Counter[str]) -> list[dict[str, object]]:
        return [
            {"value": value, "count": count}
            for value, count in sorted(counter.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
        ]

    return {
        "companies": ranked(companies),
        "categories": ranked(categories),
        "remote_count": remote_count,
        "offline": True,
    }


def _fast_snapshot_stats(
    families: list[dict[str, object]],
    health: dict[str, object],
) -> dict[str, object]:
    """Build outage-mode counters from the small materialized read model."""

    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    direct_families = [item for item in families if int(item.get("direct_openings") or 0) > 0]
    lead_families = [
        item
        for item in families
        if int(item.get("direct_openings") or 0) == 0
        and int(item.get("backstop_openings") or 0) > 0
    ]
    new_families = sum(
        1
        for item in direct_families
        if (detected := _parse_timestamp(item.get("first_detected_at"))) is not None
        and detected >= cutoff
    )
    direct_openings = sum(int(item.get("direct_openings") or 0) for item in direct_families)
    lead_openings = sum(int(item.get("backstop_openings") or 0) for item in lead_families)
    companies = len(
        {
            str(item.get("company") or "").casefold()
            for item in direct_families
            if item.get("company")
        }
    )

    inventory = health.get("inventory") if isinstance(health, dict) else None
    inventory_row = inventory if isinstance(inventory, dict) else {}
    validated_sources = int(inventory_row.get("total") or 0)

    return {
        "role_families": len(direct_families),
        "active_listings": direct_openings,
        "companies": companies,
        "new_families_today": new_families,
        "new_today": new_families,
        "new_24h": new_families,
        "new_urls_today": new_families,
        "removed_urls_today": 0,
        "verified_listings": direct_openings,
        "verified_families": len(direct_families),
        "validated_sources": validated_sources,
        "known_employers": 0,
        "enumerated_employers": 0,
        "unresolved_employers": 0,
        "blind_spots": 0,
        "leads": len(lead_families),
        "lead_apps": lead_openings,
        "snapshot_stats_mode": "materialized-families",
    }


def _inventory_state_from_connection(connection: object) -> dict[str, object]:
    row = connection.execute(  # type: ignore[attr-defined]
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
            COUNT(*) FILTER (WHERE target.lease_expires_at > now()) AS running,
            COUNT(*) FILTER (WHERE target.last_complete_at IS NULL) AS never_completed,
            COUNT(*) FILTER (
                WHERE target.last_complete_at IS NOT NULL
                  AND target.last_complete_at < now() - make_interval(secs => target.freshness_seconds)
            ) AS overdue,
            COUNT(*) FILTER (WHERE target.last_status = ANY(%s)) AS degraded,
            COUNT(*) FILTER (
                WHERE target.last_complete_at IS NOT NULL
                  AND target.last_complete_at >= now() - make_interval(secs => target.freshness_seconds)
                  AND target.last_status <> ALL(%s)
            ) AS fresh,
            COUNT(*) FILTER (
                WHERE target.last_complete_at IS NULL
                   OR target.last_complete_at < now() - make_interval(secs => target.freshness_seconds)
                   OR target.last_status = ANY(%s)
            ) AS unhealthy,
            MAX(target.last_finished_at) AS latest_activity_at,
            MIN(target.last_complete_at) FILTER (WHERE target.last_complete_at IS NOT NULL) AS coverage_watermark
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
    historical = connection.execute(  # type: ignore[attr-defined]
        """
        SELECT COUNT(*) AS count
        FROM crawl_targets AS target
        JOIN source_catalog AS catalog USING(source)
        WHERE target.scheduled
          AND catalog.validated
          AND catalog.scope='historical'
        """
    ).fetchone()
    state: dict[str, object] = {key: row[key] for key in row.keys()}
    for key in ("total", "running", "never_completed", "overdue", "degraded", "fresh", "unhealthy"):
        state[key] = int(state.get(key) or 0)
    state["historical"] = int(historical["count"] or 0)
    state["latest_activity_at"] = iso(state.get("latest_activity_at"))
    state["coverage_watermark"] = iso(state.get("coverage_watermark"))
    state["freshness_floor_seconds"] = FRESHNESS_FLOOR_SECONDS
    total = int(state["total"])
    state["fresh_percent"] = round(100 * int(state["fresh"]) / total, 1) if total else 0.0
    state["healthy"] = bool(total) and int(state["unhealthy"]) == 0
    return state


def _health_from_inventory(inventory: dict[str, object], generated_at: str) -> dict[str, object]:
    fully_initialized = int(inventory.get("never_completed") or 0) == 0 and int(
        inventory.get("total") or 0
    ) > 0
    watermark = inventory.get("coverage_watermark") if fully_initialized else None
    failing = int(inventory.get("unhealthy") or 0)
    running = int(inventory.get("running") or 0) > 0
    return {
        "ok": bool(inventory.get("healthy")),
        "read_only": os.getenv("GAIA_READ_ONLY", "0") == "1",
        "running": running,
        "stale": False,
        "generated_at": generated_at,
        "progress": {
            "mode": "continuous-inventory",
            "stage": "crawling" if running else "scheduled",
            "completed": int(inventory.get("fresh") or 0),
            "total": int(inventory.get("total") or 0),
            "current": None,
            "started_at": None,
            "elapsed_seconds": 0,
        },
        "last_summary": None,
        "data": {
            "last_run": (
                {
                    "finished_at": watermark,
                    "status": "ok" if inventory.get("healthy") else "degraded",
                }
                if watermark
                else None
            ),
            "last_success_at": watermark,
            "sources": int(inventory.get("total") or 0),
            "failing_sources": failing,
        },
        "inventory": inventory,
    }


def _direct_family_index(connection: object) -> tuple[list[dict[str, object]], int, bool]:
    """Read the family table through bounded primary-key pages.

    The snapshot is sorted in memory for every offline view, so a database-side global
    activity sort is wasted work and can spill or time out under production pressure.
    Keyset paging turns the export into small predictable index walks and avoids OFFSET.
    """

    page_size = max(32, min(1000, int(os.getenv("GAIA_STATIC_SNAPSHOT_FAMILY_PAGE_SIZE", "256"))))
    last_key = ""
    items: list[dict[str, object]] = []
    tech_categories = set(legacy.TECH_CATEGORIES)

    while True:
        rows = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT *
            FROM families
            WHERE family_key > %s
            ORDER BY family_key
            LIMIT %s
            """,
            (last_key, page_size),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            if str(row["category"]) not in tech_categories:
                continue
            items.append(_compact_family(legacy._present_family(row, trust="all")))  # noqa: SLF001
        last_key = str(rows[-1]["family_key"])
        if len(rows) < page_size:
            break

    return items, len(items), True


def _responses_from_index(
    family_index: list[dict[str, object]], health: dict[str, object]
) -> dict[str, object]:
    responses: dict[str, object] = {
        "/api/health": health,
        "/api/stats": _fast_snapshot_stats(family_index, health),
        "/api/facets": _facets_from_index(family_index),
        _key("/api/facets", trust="verified"): _facets_from_index(
            family_index, trust="verified"
        ),
        _key("/api/facets", target="exact", trust="verified"): _facets_from_index(
            family_index, trust="verified", target="exact"
        ),
    }
    for page in range(1, 6):
        params = {} if page == 1 else {"page": page}
        responses[_key("/api/families", **params)] = _family_page_from_index(
            family_index, page=page
        )
    presets = [
        {"posted_within": 1, "trust": "verified"},
        {"target": "exact", "trust": "verified"},
        {"category": "software", "target": "default", "trust": "verified"},
        {"category": "quant", "target": "default", "trust": "verified"},
        {"remote": True, "trust": "verified"},
    ]
    for params in presets:
        responses[_key("/api/families", **params)] = _family_page_from_index(
            family_index, **params
        )
    return responses


def _snapshot_stats(payload: dict[str, object]) -> dict[str, object]:
    responses = payload.get("responses")
    if not isinstance(responses, dict):
        return {}
    stats = responses.get("/api/stats")
    return stats if isinstance(stats, dict) else {}


def _validate_snapshot(payload: dict[str, object], previous: dict[str, object] | None = None) -> None:
    """Reject snapshots that are internally impossible or represent a catastrophic collapse."""

    stats = _snapshot_stats(payload)
    active = int(stats.get("active_listings") or 0)
    companies = int(stats.get("companies") or 0)
    new_today = int(stats.get("new_today") or stats.get("new_24h") or 0)
    minimum = max(1, int(os.getenv("GAIA_STATIC_SNAPSHOT_MIN_ACTIVE_LISTINGS", "100")))

    errors: list[str] = []
    if active < minimum:
        errors.append(f"active_listings={active} is below minimum={minimum}")
    if companies > active:
        errors.append(f"companies={companies} exceeds active_listings={active}")
    if new_today > active:
        errors.append(f"new_today={new_today} exceeds active_listings={active}")
    if not bool(payload.get("family_index_complete")):
        errors.append(
            f"family_index incomplete: {len(payload.get('family_index') or [])}/"
            f"{int(payload.get('family_index_total') or 0)}"
        )

    previous_active = int(_snapshot_stats(previous or {}).get("active_listings") or 0)
    if previous_active >= minimum:
        retained_fraction = float(os.getenv("GAIA_STATIC_SNAPSHOT_MIN_RETAINED_FRACTION", "0.5"))
        retained_floor = int(previous_active * max(0.0, min(retained_fraction, 1.0)))
        if active < retained_floor:
            errors.append(
                f"active_listings collapsed from {previous_active} to {active}; "
                f"required at least {retained_floor}"
            )

    if errors:
        raise RuntimeError("refusing degraded inventory snapshot: " + "; ".join(errors))


def _build_snapshot_via_live_api() -> dict[str, object]:
    """Compatibility path used when no direct DB URL exists (primarily unit tests)."""
    health = live_health()
    family_index, family_index_total, family_index_complete = _family_index()
    stats = _fast_snapshot_stats(family_index, health)

    responses: dict[str, object] = {
        "/api/health": health,
        "/api/stats": stats,
        "/api/facets": live_facets(),
        _key("/api/facets", trust="verified"): live_facets(trust="verified"),
        _key("/api/facets", target="exact", trust="verified"): live_facets(
            trust="verified", target="exact"
        ),
    }

    for page in range(1, 6):
        params = {} if page == 1 else {"page": page}
        responses[_key("/api/families", **params)] = _families(page=page)

    presets = [
        {"posted_within": 1, "trust": "verified"},
        {"target": "exact", "trust": "verified"},
        {"category": "software", "target": "default", "trust": "verified"},
        {"category": "quant", "target": "default", "trust": "verified"},
        {"remote": True, "trust": "verified"},
    ]
    for params in presets:
        responses[_key("/api/families", **params)] = _families(**params)

    inventory = health.get("inventory", {}) if isinstance(health, dict) else {}
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_activity_at": inventory.get("latest_activity_at")
        if isinstance(inventory, dict)
        else None,
        "max_stale_seconds": 86_400,
        "family_index": family_index,
        "family_index_total": family_index_total,
        "family_index_complete": family_index_complete,
        "responses": responses,
    }


def _build_snapshot_single_session() -> dict[str, object]:
    attempts = max(1, min(6, int(os.getenv("GAIA_STATIC_SNAPSHOT_DB_ATTEMPTS", "4"))))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with legacy.db.connect() as connection:
                inventory = _inventory_state_from_connection(connection)
                family_index, family_index_total, family_index_complete = _direct_family_index(
                    connection
                )
            generated_at = datetime.now(UTC).isoformat()
            health = _health_from_inventory(inventory, generated_at)
            return {
                "schema_version": 2,
                "generated_at": generated_at,
                "source_activity_at": inventory.get("latest_activity_at"),
                "max_stale_seconds": 86_400,
                "family_index": family_index,
                "family_index_total": family_index_total,
                "family_index_complete": family_index_complete,
                "responses": _responses_from_index(family_index, health),
            }
        except (psycopg.Error, OSError, TimeoutError) as error:
            last_error = error
            if attempt >= attempts:
                break
            time.sleep(min(8, 2 * attempt))
    assert last_error is not None
    raise last_error


def build_snapshot() -> dict[str, object]:
    # Production snapshot publication must not repeatedly check out Supabase sessions.
    # One successful checkout reads health + the complete materialized family table and
    # all fallback responses are derived in memory. The legacy path remains for tests
    # and local environments where PostgreSQL is intentionally not configured.
    if getattr(legacy.db, "url", None):
        return _build_snapshot_single_session()
    return _build_snapshot_via_live_api()


def write_snapshot(path: Path = DEFAULT_OUTPUT) -> Path:
    payload = build_snapshot()
    previous: dict[str, object] | None = None
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, ValueError):
            previous = None
    _validate_snapshot(payload, previous)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a deployable last-known GAIA inventory snapshot"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = write_snapshot(args.output)
    print(output)


if __name__ == "__main__":
    main()
