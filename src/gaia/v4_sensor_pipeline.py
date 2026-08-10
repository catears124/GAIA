from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .models import Posting
from .v4_invariants import validate_sensor_recall
from .v4_market_filter import is_current_market_target, normalize_sensor_postings
from .v4_migrate import sanitize_previous_snapshot
from .v4_openroles_sensor import fetch_all_market_sensors
from .v4_pipeline import (
    _build_families,
    _health,
    _incoming_observations,
    _previous_first_seen,
    _previous_observations,
    _validate,
)
from .v4_snapshot import responses as snapshot_responses
from .v4_snapshot import stats as snapshot_stats

DEFAULT_INVENTORY = Path(__file__).with_name("frontend") / "last-known-inventory.json"


async def run(
    *,
    previous_path: Path,
    output_path: Path,
    sensor_concurrency: int,
) -> dict[str, object]:
    """Refresh the public market view without waiting for employer verification.

    Previous verified observations are retained under their direct-evidence TTL.
    Successful sensors atomically replace only their own previous observations. A
    pre-v4 snapshot is first stripped of legacy leads whose old shape cannot prove
    direct employer verification.
    """
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    if not isinstance(previous, dict):
        previous = {}
    previous, migrated_legacy = sanitize_previous_snapshot(previous)
    started_at = datetime.now(UTC)

    sensor_raw, sensor_runs = await fetch_all_market_sensors(concurrency=sensor_concurrency)
    normalized = normalize_sensor_postings(sensor_raw)
    sensor_postings = [posting for posting in normalized if is_current_market_target(posting)]
    sensor_by_url: dict[str, list[Posting]] = defaultdict(list)
    for posting in sensor_postings:
        sensor_by_url[posting.canonical_apply_url].append(posting)

    now = datetime.now(UTC)
    first_seen = _previous_first_seen(previous)
    previous_records = _previous_observations(previous, now)
    successful_sensor_sources = {f"sensor:{run.name}" for run in sensor_runs if run.status == "ok"}
    preserved = [
        record
        for record in previous_records
        if record.posting.source not in successful_sensor_sources
    ]
    incoming = _incoming_observations(
        sensor_postings,
        first_seen_by_url=first_seen,
        now=now,
    )
    families = _build_families([*preserved, *incoming])
    _validate(families, sensor_runs)
    sensor_recall = validate_sensor_recall(sensor_postings, families, now=now)

    generated_at = datetime.now(UTC)
    verify_summary: dict[str, object] = {
        "selected": 0,
        "refreshed": 0,
        "postings": 0,
        "statuses": {},
        "failures": [],
        "deferred": True,
    }
    health = _health(
        runs=sensor_runs,
        families=families,
        verify_summary=verify_summary,
        generated_at=generated_at,
    )
    payload: dict[str, object] = {
        "schema_version": 4,
        "generated_at": generated_at.isoformat(),
        "source_activity_at": health["inventory"]["latest_activity_at"],  # type: ignore[index]
        "max_stale_seconds": 900,
        "family_index": families,
        "family_index_total": len(families),
        "family_index_complete": True,
        "responses": snapshot_responses(families, health),
        "v4": {
            "mode": "market-sensor-pulse",
            "objective": "publish new relevant market detections before verification latency can hide them",
            "started_at": started_at.isoformat(),
            "sensor_postings": len(sensor_postings),
            "sensor_unique_urls": len(sensor_by_url),
            "sensor_recall": sensor_recall,
            "verification_deferred": True,
            "legacy_snapshot_sanitized": migrated_legacy,
            "stats": snapshot_stats(families),
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")
    temporary.replace(output_path)

    return {
        "mode": "market-sensor-pulse",
        "families": len(families),
        "sensors_ok": sum(run.status == "ok" for run in sensor_runs),
        "sensors_total": len(sensor_runs),
        "sensor_postings": len(sensor_postings),
        "sensor_unique_urls": len(sensor_by_url),
        "sensor_recall": sensor_recall,
        "legacy_snapshot_sanitized": migrated_legacy,
        "stats": snapshot_stats(families),
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish GAIA v4 market detections without blocking on verification")
    parser.add_argument("--previous", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--sensor-concurrency",
        type=int,
        default=int(os.getenv("GAIA_V4_SENSOR_CONCURRENCY", "8")),
    )
    args = parser.parse_args()
    summary = asyncio.run(
        run(
            previous_path=args.previous,
            output_path=args.output,
            sensor_concurrency=max(1, args.sensor_concurrency),
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
