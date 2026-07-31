from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from .product_api import live_facets, live_families, live_health, live_stats

DEFAULT_OUTPUT = Path(__file__).with_name("frontend") / "last-known-inventory.json"


def _key(path: str, **params: object) -> str:
    values = [(name, str(value).lower() if isinstance(value, bool) else str(value)) for name, value in params.items() if value not in (None, "", False, 0)]
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


def build_snapshot() -> dict[str, object]:
    responses: dict[str, object] = {
        "/api/health": live_health(),
        "/api/stats": live_stats(),
        "/api/facets": live_facets(),
        _key("/api/facets", trust="verified"): live_facets(trust="verified"),
        _key("/api/facets", target="exact", trust="verified"): live_facets(trust="verified", target="exact"),
    }

    # Keep enough default pages for useful first-visit browsing during a database outage.
    for page in range(1, 6):
        params = {} if page == 1 else {"page": page}
        responses[_key("/api/families", **params)] = _families(page=page)

    presets = [
        ({"posted_within": 1, "trust": "verified"}, "new verified"),
        ({"target": "exact", "trust": "verified"}, "summer 2027"),
        ({"category": "software", "target": "default", "trust": "verified"}, "software 2027"),
        ({"category": "quant", "target": "default", "trust": "verified"}, "quant 2027"),
        ({"remote": True, "trust": "verified"}, "remote verified"),
    ]
    for params, _label in presets:
        responses[_key("/api/families", **params)] = _families(**params)

    health = responses["/api/health"]
    inventory = health.get("inventory", {}) if isinstance(health, dict) else {}
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_activity_at": inventory.get("latest_activity_at") if isinstance(inventory, dict) else None,
        "max_stale_seconds": 86_400,
        "responses": responses,
    }


def write_snapshot(path: Path = DEFAULT_OUTPUT) -> Path:
    payload = build_snapshot()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")
    temporary.replace(path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a deployable last-known GAIA inventory snapshot")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = write_snapshot(args.output)
    print(output)


if __name__ == "__main__":
    main()
