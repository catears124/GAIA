from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from . import v4_pipeline
from .v4_market_filter import is_current_market_target, normalize_sensor_postings
from .v4_migrate import sanitize_previous_snapshot

DEFAULT_INVENTORY = v4_pipeline.DEFAULT_INVENTORY


async def run(
    *,
    previous_path: Path,
    output_path: Path,
    sensor_concurrency: int,
    verify_concurrency: int,
) -> dict[str, object]:
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    if not isinstance(previous, dict):
        previous = {}
    sanitized, migrated = sanitize_previous_snapshot(previous)

    # The heavy verifier reuses v4_pipeline, but the public product now ingests the
    # whole active technical-internship market and applies Summer 2027 only as a
    # query filter. Patch the verifier's sensor boundary to the exact same contract
    # so it cannot silently shrink back to Summer-2027-only while enriching leads.
    original_fetch = v4_pipeline.fetch_all_sensors
    original_target = v4_pipeline.is_default_target

    async def filtered_fetch(*args, **kwargs):
        postings, runs = await original_fetch(*args, **kwargs)
        return normalize_sensor_postings(postings), runs

    v4_pipeline.fetch_all_sensors = filtered_fetch
    v4_pipeline.is_default_target = is_current_market_target
    try:
        with tempfile.TemporaryDirectory(prefix="gaia-v4-verify-") as directory:
            safe_previous = Path(directory) / "previous.json"
            safe_previous.write_text(json.dumps(sanitized, separators=(",", ":")), encoding="utf-8")
            summary = await v4_pipeline.run(
                previous_path=safe_previous,
                output_path=output_path,
                sensor_concurrency=sensor_concurrency,
                verify_concurrency=verify_concurrency,
            )
    finally:
        v4_pipeline.fetch_all_sensors = original_fetch
        v4_pipeline.is_default_target = original_target

    summary["legacy_snapshot_sanitized"] = migrated
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GAIA v4 employer verification from a trust-safe previous snapshot")
    parser.add_argument("--previous", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--sensor-concurrency",
        type=int,
        default=int(os.getenv("GAIA_V4_SENSOR_CONCURRENCY", "8")),
    )
    parser.add_argument(
        "--verify-concurrency",
        type=int,
        default=int(os.getenv("GAIA_V4_VERIFY_CONCURRENCY", "48")),
    )
    args = parser.parse_args()
    summary = asyncio.run(
        run(
            previous_path=args.previous,
            output_path=args.output,
            sensor_concurrency=max(1, args.sensor_concurrency),
            verify_concurrency=max(1, args.verify_concurrency),
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
