from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .v4_snapshot import family_page, stats

_SNAPSHOT_PATH = Path(__file__).with_name("frontend") / "last-known-inventory.json"


@lru_cache(maxsize=1)
def load_snapshot() -> dict[str, Any]:
    return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _integer(value: str | None, default: int, *, minimum: int, maximum: int | None = None) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    return min(maximum, parsed) if maximum is not None else parsed


def _families(snapshot: dict[str, Any], request: Request) -> JSONResponse:
    params = request.query_params
    index = [item for item in snapshot.get("family_index") or [] if isinstance(item, dict)]
    page = _integer(params.get("page"), 1, minimum=1)
    page_size = _integer(params.get("page_size"), 48, minimum=12, maximum=100)
    posted_within = _integer(params.get("posted_within"), 0, minimum=0)
    payload = family_page(
        index,
        page=page,
        page_size=page_size,
        sort=params.get("sort", "newest"),
        q=params.get("q", ""),
        category=params.get("category", ""),
        target=params.get("target", ""),
        trust=params.get("trust", "all"),
        company=params.get("company", ""),
        location=params.get("location", ""),
        remote=params.get("remote", "false").casefold() == "true",
        posted_within=posted_within,
    )
    payload.update(
        {
            "stale": True,
            "snapshot_generated_at": snapshot.get("generated_at"),
            "source_activity_at": snapshot.get("source_activity_at"),
        }
    )
    return JSONResponse(
        payload,
        headers={"Cache-Control": "public, max-age=30, stale-while-revalidate=300"},
    )


def _stats(snapshot: dict[str, Any]) -> JSONResponse:
    index = [item for item in snapshot.get("family_index") or [] if isinstance(item, dict)]
    payload = stats(index)
    payload.update(
        {
            "stale": True,
            "snapshot_generated_at": snapshot.get("generated_at"),
            "source_activity_at": snapshot.get("source_activity_at"),
        }
    )
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
