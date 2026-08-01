from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from .product_api import live_facets, live_families, live_health, live_stats

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
    """Export a compact visible-family feed for offline filtering and details."""

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


def build_snapshot() -> dict[str, object]:
    responses: dict[str, object] = {
        "/api/health": live_health(),
        "/api/stats": live_stats(),
        "/api/facets": live_facets(),
        _key("/api/facets", trust="verified"): live_facets(trust="verified"),
        _key("/api/facets", target="exact", trust="verified"): live_facets(
            trust="verified", target="exact"
        ),
    }

    # Retain exact common routes for instant fallback and backwards compatibility.
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

    family_index, family_index_total, family_index_complete = _family_index()
    health = responses["/api/health"]
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