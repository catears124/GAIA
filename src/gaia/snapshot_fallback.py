from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

_SNAPSHOT_PATH = Path(__file__).with_name("frontend") / "last-known-inventory.json"
_TECH_CATEGORIES = {"software", "ml-ai", "quant", "security", "data", "product", "other-technical"}
_TARGET_MATCHES = {"exact", "year_confirmed", "source_confirmed"}


@lru_cache(maxsize=1)
def load_snapshot() -> dict[str, Any]:
    return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _activity(item: dict[str, Any]) -> datetime:
    # Employer posting time is authoritative. Discovery is only a fallback for an
    # undated role; recovering an old posting today must not make it a new posting.
    return (
        _timestamp(item.get("latest_posted_at"))
        or _timestamp(item.get("first_detected_at"))
        or datetime.min.replace(tzinfo=UTC)
    )


def _verified_activity(item: dict[str, Any]) -> datetime:
    return _timestamp(item.get("last_verified_at")) or datetime.min.replace(tzinfo=UTC)


def _verified(item: dict[str, Any]) -> bool:
    return bool(item.get("verified")) or int(item.get("direct_openings") or 0) > 0


def _matches_target(item: dict[str, Any], target: str) -> bool:
    if not target:
        return True
    match = str(item.get("target_match") or "")
    if target == "default":
        return match in _TARGET_MATCHES
    return match == target


def _sort_items(items: list[dict[str, Any]], sort: str) -> None:
    if sort == "company":
        items.sort(
            key=lambda item: (
                str(item.get("company") or "").casefold(),
                str(item.get("title") or "").casefold(),
                str(item.get("family_key") or ""),
            )
        )
        return

    # Python sorts tuples lexicographically. Negative booleans put verified and dated
    # rows first while the timestamp fields remain descending via reverse=True.
    if sort == "verified":
        items.sort(
            key=lambda item: (
                _verified(item),
                _verified_activity(item),
                item.get("latest_posted_at") is not None,
                _activity(item),
                str(item.get("family_key") or ""),
            ),
            reverse=True,
        )
        return

    items.sort(
        key=lambda item: (
            _verified(item),
            item.get("latest_posted_at") is not None,
            _activity(item),
            _verified_activity(item),
            str(item.get("family_key") or ""),
        ),
        reverse=True,
    )


def _filter_items(snapshot: dict[str, Any], request: Request) -> list[dict[str, Any]]:
    params = request.query_params
    items = [item for item in snapshot.get("family_index") or [] if isinstance(item, dict)]
    track = params.get("track", "tech")
    trust = params.get("trust", "all")
    query_tokens = [token for token in params.get("q", "").strip().casefold().split() if token]
    company = params.get("company", "").strip().casefold()
    category = params.get("category", "").strip()
    location = params.get("location", "").strip().casefold()
    target = params.get("target", "").strip()
    remote = params.get("remote", "false").casefold() == "true"
    try:
        posted_within = max(0, int(params.get("posted_within", "0") or 0))
    except ValueError:
        posted_within = 0
    cutoff = datetime.now(UTC) - timedelta(days=posted_within) if posted_within else None

    filtered: list[dict[str, Any]] = []
    for item in items:
        locations = [str(value) for value in item.get("locations") or []]
        location_text = " ".join(locations).casefold()
        haystack = f"{item.get('company', '')} {item.get('title', '')} {location_text}".casefold()
        is_verified = _verified(item)

        if track == "tech" and item.get("category") not in _TECH_CATEGORIES:
            continue
        if trust == "verified" and not is_verified:
            continue
        if trust == "leads" and is_verified:
            continue
        if category and item.get("category") != category:
            continue
        if not _matches_target(item, target):
            continue
        if company and str(item.get("company") or "").casefold() != company:
            continue
        if query_tokens and any(token not in haystack for token in query_tokens):
            continue
        if location and location not in location_text:
            continue
        if remote and not (bool(item.get("remote")) or "remote" in location_text):
            continue
        if cutoff and _activity(item) < cutoff:
            continue
        filtered.append(item)
    return filtered


def _families(snapshot: dict[str, Any], request: Request) -> JSONResponse:
    params = request.query_params
    items = _filter_items(snapshot, request)
    _sort_items(items, params.get("sort", "newest"))

    try:
        page = max(1, int(params.get("page", "1")))
    except ValueError:
        page = 1
    try:
        page_size = min(100, max(12, int(params.get("page_size", "48"))))
    except ValueError:
        page_size = 48
    total = len(items)
    start = (page - 1) * page_size
    payload = {
        "items": items[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "stale": True,
        "snapshot_generated_at": snapshot.get("generated_at"),
        "source_activity_at": snapshot.get("source_activity_at"),
    }
    return JSONResponse(
        payload,
        headers={"Cache-Control": "public, max-age=30, stale-while-revalidate=300"},
    )


def _stats(snapshot: dict[str, Any]) -> JSONResponse:
    items = [
        item
        for item in snapshot.get("family_index") or []
        if isinstance(item, dict) and _verified(item) and item.get("category") in _TECH_CATEGORIES
    ]
    companies = {str(item.get("company", "")).casefold() for item in items if item.get("company")}
    active = sum(int(item.get("direct_openings") or 0) for item in items)
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    new_families = sum(
        1
        for item in items
        if (detected := _timestamp(item.get("first_detected_at"))) is not None and detected >= cutoff
    )
    payload = {
        "role_families": len(items),
        "active_listings": active,
        "companies": len(companies),
        "new_today": new_families,
        "new_24h": new_families,
        "new_families_24h": new_families,
        "verified_listings": active,
        "verified_families": len(items),
        "activity_units": {"new_today": "role_family", "url_movement": "canonical_apply_url"},
        "stale": True,
        "snapshot_generated_at": snapshot.get("generated_at"),
        "source_activity_at": snapshot.get("source_activity_at"),
    }
    return JSONResponse(
        payload,
        headers={"Cache-Control": "public, max-age=30, stale-while-revalidate=300"},
    )


def snapshot_response(request: Request) -> JSONResponse | None:
    try:
        snapshot = load_snapshot()
        if request.url.path == "/api/families":
            return _families(snapshot, request)
        if request.url.path == "/api/stats":
            return _stats(snapshot)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None
