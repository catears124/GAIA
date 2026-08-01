from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

_SNAPSHOT_PATH = Path(__file__).with_name("frontend") / "last-known-inventory.json"
_TECH_CATEGORIES = {"software", "ml-ai", "quant", "security", "data", "product", "other-technical"}


@lru_cache(maxsize=1)
def load_snapshot() -> dict[str, Any]:
    return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _families(snapshot: dict[str, Any], request: Request) -> JSONResponse:
    params = request.query_params
    items = list(snapshot.get("family_index") or [])
    track = params.get("track", "tech")
    trust = params.get("trust", "all")
    query = params.get("q", "").strip().casefold()
    company = params.get("company", "").strip().casefold()
    category = params.get("category", "").strip()
    location = params.get("location", "").strip().casefold()

    if track == "tech":
        items = [item for item in items if item.get("category") in _TECH_CATEGORIES]
    if trust == "verified":
        items = [item for item in items if item.get("verified")]
    if category:
        items = [item for item in items if item.get("category") == category]
    if company:
        items = [item for item in items if company in str(item.get("company", "")).casefold()]
    if query:
        items = [
            item
            for item in items
            if query in f"{item.get('company', '')} {item.get('title', '')}".casefold()
        ]
    if location:
        items = [
            item
            for item in items
            if any(location in str(value).casefold() for value in item.get("locations") or [])
        ]

    sort = params.get("sort", "newest")
    if sort == "company":
        items.sort(key=lambda item: (str(item.get("company", "")).casefold(), str(item.get("title", "")).casefold()))
    elif sort == "verified":
        items.sort(key=lambda item: str(item.get("last_verified_at") or ""), reverse=True)
    else:
        items.sort(
            key=lambda item: str(item.get("latest_posted_at") or item.get("first_detected_at") or ""),
            reverse=True,
        )

    page = max(1, int(params.get("page", "1")))
    page_size = min(100, max(12, int(params.get("page_size", "48"))))
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
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=30, stale-while-revalidate=300"})


def _stats(snapshot: dict[str, Any]) -> JSONResponse:
    items = [item for item in snapshot.get("family_index") or [] if item.get("verified")]
    tech = [item for item in items if item.get("category") in _TECH_CATEGORIES]
    companies = {str(item.get("company", "")).casefold() for item in tech if item.get("company")}
    active = sum(int(item.get("direct_openings") or 0) for item in tech)
    payload = {
        "role_families": len(tech),
        "active_listings": active,
        "companies": len(companies),
        "new_today": 0,
        "new_24h": 0,
        "new_families_24h": 0,
        "verified_listings": active,
        "verified_families": len(tech),
        "activity_units": {"new_today": "role_family", "url_movement": "canonical_apply_url"},
        "stale": True,
        "snapshot_generated_at": snapshot.get("generated_at"),
        "source_activity_at": snapshot.get("source_activity_at"),
    }
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=30, stale-while-revalidate=300"})


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
