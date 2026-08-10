from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

TARGET_MATCHES = {"exact", "year_confirmed", "source_confirmed"}
TECH_CATEGORIES = {"software", "ml-ai", "quant", "security", "data", "product", "hardware", "other-technical"}


def timestamp(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def activity(item: dict[str, object]) -> datetime:
    """The market timestamp drives recency. Verification never drives recency."""
    return (
        timestamp(item.get("market_event_at"))
        or timestamp(item.get("latest_posted_at"))
        or timestamp(item.get("latest_sensor_reported_at"))
        or timestamp(item.get("market_first_seen_at"))
        or timestamp(item.get("first_detected_at"))
        or datetime.min.replace(tzinfo=UTC)
    )


def verified_activity(item: dict[str, object]) -> datetime:
    return timestamp(item.get("last_verified_at")) or datetime.min.replace(tzinfo=UTC)


def verified(item: dict[str, object]) -> bool:
    return bool(item.get("verified")) or int(item.get("direct_openings") or 0) > 0


def _matches_target(item: dict[str, object], target: str) -> bool:
    if not target:
        return True
    match = str(item.get("target_match") or "")
    if target == "default":
        return match in TARGET_MATCHES
    return match == target


def filter_families(
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
        if item.get("category") not in TECH_CATEGORIES:
            continue
        locations = [str(value) for value in item.get("locations") or []]
        location_text = " ".join(locations).casefold()
        haystack = f"{item.get('title') or ''} {item.get('company') or ''} {location_text}".casefold()
        is_verified = verified(item)
        if any(token not in haystack for token in tokens):
            continue
        if category and str(item.get("category") or "") != category:
            continue
        if not _matches_target(item, target):
            continue
        if trust == "verified" and not is_verified:
            continue
        if trust == "leads" and is_verified:
            continue
        if company_query and str(item.get("company") or "").casefold() != company_query:
            continue
        if location_query and location_query not in location_text:
            continue
        if remote and not (bool(item.get("remote")) or "remote" in location_text):
            continue
        if cutoff and activity(item) < cutoff:
            continue
        result.append(item)
    return result


def sort_families(items: list[dict[str, object]], sort: str = "newest") -> None:
    if sort == "company":
        items.sort(
            key=lambda item: (
                str(item.get("company") or "").casefold(),
                str(item.get("title") or "").casefold(),
                str(item.get("family_key") or ""),
            )
        )
        return
    if sort == "verified":
        items.sort(
            key=lambda item: (verified_activity(item), activity(item), str(item.get("family_key") or "")),
            reverse=True,
        )
        return

    # First principle: "newest" means newest market event. Trust is a secondary
    # tiebreaker, not a reason to bury a ten-minute-old lead below a week-old role.
    items.sort(
        key=lambda item: (activity(item), verified(item), verified_activity(item), str(item.get("family_key") or "")),
        reverse=True,
    )


def family_page(
    index: list[dict[str, object]],
    *,
    page: int = 1,
    page_size: int = 48,
    sort: str = "newest",
    **filters: object,
) -> dict[str, object]:
    items = filter_families(index, **filters)  # type: ignore[arg-type]
    sort_families(items, sort)
    start = max(0, page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "total": len(items),
        "page": page,
        "page_size": page_size,
        "offline": True,
    }


def facets(index: list[dict[str, object]], *, trust: str = "all", target: str = "") -> dict[str, object]:
    items = filter_families(index, trust=trust, target=target)
    companies = Counter(str(item.get("company") or "") for item in items if item.get("company"))
    categories = Counter(str(item.get("category") or "") for item in items if item.get("category"))

    def ranked(counter: Counter[str]) -> list[dict[str, object]]:
        return [
            {"value": value, "count": count}
            for value, count in sorted(counter.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
        ]

    return {
        "companies": ranked(companies),
        "categories": ranked(categories),
        "remote_count": sum(1 for item in items if bool(item.get("remote"))),
        "offline": True,
    }


def stats(index: list[dict[str, object]]) -> dict[str, object]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    direct = [item for item in index if verified(item) and item.get("category") in TECH_CATEGORIES]
    leads = [item for item in index if not verified(item) and item.get("category") in TECH_CATEGORIES]
    all_visible = [item for item in index if item.get("category") in TECH_CATEGORIES]
    active = sum(int(item.get("direct_openings") or 0) for item in direct)
    lead_apps = sum(int(item.get("backstop_openings") or 0) for item in leads)
    companies = {str(item.get("company") or "").casefold() for item in direct if item.get("company")}
    new_verified = sum(1 for item in direct if activity(item) >= cutoff)
    market_events = sum(1 for item in all_visible if activity(item) >= cutoff)
    discoveries = sum(
        1
        for item in all_visible
        if (seen := timestamp(item.get("market_first_seen_at") or item.get("first_detected_at"))) is not None
        and seen >= cutoff
    )
    backlog = sum(1 for item in leads if activity(item) >= now - timedelta(days=14))
    return {
        "role_families": len(direct),
        "active_listings": active,
        "companies": len(companies),
        "new_today": new_verified,
        "new_24h": new_verified,
        "new_verified_24h": new_verified,
        "market_events_24h": market_events,
        "discovered_24h": discoveries,
        "verified_listings": active,
        "verified_families": len(direct),
        "leads": len(leads),
        "lead_apps": lead_apps,
        "verification_backlog": backlog,
        "activity_units": {
            "new_today": "verified_role_family_with_market_event_in_24h",
            "market_events_24h": "role_family",
            "discovered_24h": "role_family",
        },
        "snapshot_stats_mode": "v4-market-first",
    }


def _key(path: str, **params: object) -> str:
    values = [
        (name, str(value).lower() if isinstance(value, bool) else str(value))
        for name, value in params.items()
        if value not in (None, "", False, 0)
    ]
    values.sort()
    query = urlencode(values)
    return f"{path}?{query}" if query else path


def responses(index: list[dict[str, object]], health: dict[str, object]) -> dict[str, object]:
    output: dict[str, object] = {
        "/api/health": health,
        "/api/stats": stats(index),
        "/api/facets": facets(index),
        _key("/api/facets", trust="verified"): facets(index, trust="verified"),
        _key("/api/facets", target="exact", trust="verified"): facets(index, trust="verified", target="exact"),
    }
    for page in range(1, 6):
        params = {} if page == 1 else {"page": page}
        output[_key("/api/families", **params)] = family_page(index, page=page)
    for params in (
        {"posted_within": 1, "trust": "verified"},
        {"target": "exact", "trust": "verified"},
        {"category": "software", "target": "default", "trust": "verified"},
        {"category": "quant", "target": "default", "trust": "verified"},
        {"remote": True, "trust": "verified"},
    ):
        output[_key("/api/families", **params)] = family_page(index, **params)
    return output
