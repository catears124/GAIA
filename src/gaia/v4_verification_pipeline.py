from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from .v4_migrate import sanitize_previous_snapshot
from .v4_pipeline import DEFAULT_INVENTORY, run as run_verification


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

    with tempfile.TemporaryDirectory(prefix="gaia-v4-verify-") as directory:
        safe_previous = Path(directory) / "previous.json"
        safe_previous.write_text(json.dumps(sanitized, separators=(",", ":")), encoding="utf-8")
        summary = await run_verification(
            previous_path=safe_previous,
            output_path=output_path,
            sensor_concurrency=sensor_concurrency,
            verify_concurrency=verify_concurrency,
        )
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
