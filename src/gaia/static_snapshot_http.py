from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .static_snapshot import DEFAULT_OUTPUT, _compact_family


class SnapshotExportError(RuntimeError):
    pass


def _request(client: httpx.Client, path: str, **params: object) -> dict[str, object]:
    query = {
        key: str(value).lower() if isinstance(value, bool) else value
        for key, value in params.items()
        if value not in (None, "", False, 0)
    }
    response = client.get(path, params=query)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise SnapshotExportError(f"{path} returned {type(payload).__name__}, expected object")
    return payload


def _key(path: str, **params: object) -> str:
    values = [
        (name, str(value).lower() if isinstance(value, bool) else str(value))
        for name, value in params.items()
        if value not in (None, "", False, 0)
    ]
    values.sort()
    query = urlencode(values)
    return f"{path}?{query}" if query else path


def _integer(value: object, *, name: str) -> int:
    if isinstance(value, bool):
        raise SnapshotExportError(f"public health endpoint returned boolean {name}")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise SnapshotExportError(f"public health endpoint returned invalid {name}") from error


def _timestamp(value: object, *, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotExportError(f"public health endpoint omitted {name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SnapshotExportError(f"public health endpoint returned invalid {name}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_live_health(
    health: dict[str, object],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Accept fresh degraded inventory, but never stale, empty, or dishonest data."""

    if health.get("stale") is True:
        raise SnapshotExportError("public health endpoint returned stale inventory")
    if not isinstance(health.get("ok"), bool):
        raise SnapshotExportError("public health endpoint omitted boolean ok state")
    inventory = health.get("inventory")
    if not isinstance(inventory, dict):
        raise SnapshotExportError("public health endpoint omitted inventory")
    if not isinstance(inventory.get("healthy"), bool):
        raise SnapshotExportError("public inventory omitted boolean healthy state")
    if health["ok"] is True and inventory["healthy"] is not True:
        raise SnapshotExportError("public health endpoint dishonestly reports ok")

    total = _integer(inventory.get("total"), name="inventory total")
    fresh = _integer(inventory.get("fresh"), name="fresh inventory count")
    if total <= 0:
        raise SnapshotExportError("public health endpoint reported empty inventory")
    if fresh <= 0 or fresh > total:
        raise SnapshotExportError(
            f"public health endpoint reported invalid freshness fresh={fresh} total={total}"
        )

    activity = _timestamp(inventory.get("latest_activity_at"), name="latest inventory activity")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    max_age_minutes = max(
        15,
        min(int(os.getenv("GAIA_STATIC_SNAPSHOT_MAX_ACTIVITY_MINUTES", "360")), 1440),
    )
    if activity > current + timedelta(minutes=5):
        raise SnapshotExportError("public inventory activity timestamp is in the future")
    if current - activity > timedelta(minutes=max_age_minutes):
        raise SnapshotExportError(
            f"public inventory activity is older than {max_age_minutes} minutes"
        )
    return inventory


def _family_total(payload: dict[str, object], *, page: int) -> int:
    raw_total = payload.get("total")
    if isinstance(raw_total, bool):
        raise SnapshotExportError(f"families page {page} returned boolean total")
    try:
        total = int(raw_total)
    except (TypeError, ValueError) as error:
        raise SnapshotExportError(f"families page {page} returned invalid total") from error
    if total <= 0:
        raise SnapshotExportError(f"families page {page} returned non-positive total")
    return total


def build_snapshot(base_url: str, *, timeout_seconds: float = 30.0) -> dict[str, object]:
    base_url = base_url.rstrip("/")
    with httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
        follow_redirects=True,
        headers={"Accept": "application/json", "User-Agent": "gaia-static-snapshot/2"},
    ) as client:
        health = _request(client, "/api/health")
        inventory = _validate_live_health(health)

        responses: dict[str, object] = {
            "/api/health": health,
            "/api/stats": _request(client, "/api/stats"),
            "/api/facets": _request(client, "/api/facets"),
            _key("/api/facets", trust="verified"): _request(
                client, "/api/facets", trust="verified"
            ),
            _key("/api/facets", target="exact", trust="verified"): _request(
                client, "/api/facets", target="exact", trust="verified"
            ),
        }

        family_index: list[dict[str, object]] = []
        seen: set[str] = set()
        expected_total: int | None = None
        max_pages = max(1, int(os.getenv("GAIA_STATIC_SNAPSHOT_MAX_PAGES", "100")))
        for page in range(1, max_pages + 1):
            payload = _request(
                client,
                "/api/families",
                page=page,
                page_size=100,
                sort="newest",
            )
            rows = payload.get("items")
            if not isinstance(rows, list):
                raise SnapshotExportError("families endpoint returned a non-list items field")
            page_total = _family_total(payload, page=page)
            if expected_total is None:
                expected_total = page_total
            elif page_total != expected_total:
                raise SnapshotExportError(
                    f"public family total changed during export: expected={expected_total} "
                    f"page={page} reported={page_total}"
                )
            if page == 1:
                responses["/api/families"] = payload
            if not rows and len(family_index) < expected_total:
                raise SnapshotExportError(
                    f"public family export ended early: exported={len(family_index)} "
                    f"expected={expected_total} page={page}"
                )
            for raw in rows:
                if not isinstance(raw, dict):
                    raise SnapshotExportError(f"families page {page} returned a non-object row")
                key = str(raw.get("family_key") or "").strip()
                if not key:
                    raise SnapshotExportError(f"families page {page} returned a blank family key")
                if key in seen:
                    raise SnapshotExportError(f"families page {page} repeated family key {key}")
                seen.add(key)
                family_index.append(_compact_family(raw))
            if len(family_index) >= expected_total:
                break

        if expected_total is None or not family_index:
            raise SnapshotExportError("public API returned no visible families")
        if len(family_index) != expected_total:
            raise SnapshotExportError(
                f"public family export incomplete: exported={len(family_index)} expected={expected_total}"
            )

        for page in range(2, 6):
            responses[_key("/api/families", page=page)] = _request(
                client, "/api/families", page=page, page_size=48, sort="newest"
            )
        presets = [
            {"posted_within": 1, "trust": "verified"},
            {"target": "exact", "trust": "verified"},
            {"category": "software", "target": "default", "trust": "verified"},
            {"category": "quant", "target": "default", "trust": "verified"},
            {"remote": True, "trust": "verified"},
        ]
        for params in presets:
            responses[_key("/api/families", **params)] = _request(
                client, "/api/families", page=1, page_size=48, sort="newest", **params
            )

    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_activity_at": inventory.get("latest_activity_at"),
        "max_stale_seconds": 86_400,
        "family_index": family_index,
        "family_index_total": expected_total,
        "family_index_complete": True,
        "responses": responses,
        "export_source": "public-api",
        "export_inventory_state": {
            "healthy": inventory.get("healthy") is True,
            "fresh": int(inventory["fresh"]),
            "total": int(inventory["total"]),
        },
    }


def write_snapshot(
    path: Path = DEFAULT_OUTPUT,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 30.0,
) -> Path:
    resolved_base = base_url or os.getenv("GAIA_PUBLIC_BASE_URL", "https://gaiajob.vercel.app")
    payload = build_snapshot(resolved_base, timeout_seconds=timeout_seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export GAIA's deployable snapshot through its public API"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--base-url",
        default=os.getenv("GAIA_PUBLIC_BASE_URL", "https://gaiajob.vercel.app"),
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    print(write_snapshot(args.output, base_url=args.base_url, timeout_seconds=args.timeout_seconds))


if __name__ == "__main__":
    main()
