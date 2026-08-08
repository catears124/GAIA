from __future__ import annotations

import argparse
import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from . import api as legacy
from .db_base import TARGET_RANK
from .static_snapshot import (
    DEFAULT_OUTPUT,
    _compact_family,
    _responses_from_index,
    _validate_snapshot,
)


def _load_previous(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _snapshot_health(
    previous: dict[str, object] | None,
    generated_at: str,
) -> dict[str, object]:
    """Carry last-known crawler telemetry without making it a snapshot dependency."""

    responses = previous.get("responses") if isinstance(previous, dict) else None
    previous_health = responses.get("/api/health") if isinstance(responses, dict) else None
    if isinstance(previous_health, dict):
        health = deepcopy(previous_health)
    else:
        health = {
            "read_only": True,
            "running": False,
            "progress": {
                "mode": "snapshot",
                "stage": "snapshot",
                "completed": 0,
                "total": 0,
                "current": None,
                "started_at": None,
                "elapsed_seconds": 0,
            },
            "data": {
                "last_run": None,
                "last_success_at": None,
                "sources": 0,
                "failing_sources": 0,
            },
            "inventory": {
                "total": 0,
                "fresh": 0,
                "unhealthy": 0,
                "fresh_percent": 0.0,
                "latest_activity_at": None,
            },
        }

    health["ok"] = False
    health["stale"] = True
    health["running"] = False
    health["generated_at"] = generated_at
    inventory = health.get("inventory")
    if not isinstance(inventory, dict):
        inventory = {}
    inventory = dict(inventory)
    inventory["healthy"] = False
    inventory["stale_snapshot"] = True
    health["inventory"] = inventory
    return health


def _source_activity(
    previous: dict[str, object] | None,
    health: dict[str, object],
) -> object:
    inventory = health.get("inventory")
    if isinstance(inventory, dict) and inventory.get("latest_activity_at"):
        return inventory["latest_activity_at"]
    if isinstance(previous, dict):
        return previous.get("source_activity_at")
    return None


def _indexed_family_index(connection: object) -> tuple[list[dict[str, object]], int, bool]:
    """Read only technical families through the existing feed index.

    idx_families_feed starts with (target_match, category). Splitting the export by
    target_match lets PostgreSQL satisfy each small read from that index instead of
    scanning/sorting the entire family table or walking irrelevant family keys.
    """

    categories = list(legacy.TECH_CATEGORIES)
    targets = [value for value in TARGET_RANK if value != "not_internship"]
    items: list[dict[str, object]] = []
    for target in targets:
        started = time.monotonic()
        rows = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT *
            FROM families
            WHERE target_match=%s
              AND category = ANY(%s)
            """,
            (target, categories),
        ).fetchall()
        print(
            f"snapshot family bucket target={target} rows={len(rows)} "
            f"elapsed={time.monotonic() - started:.3f}s",
            flush=True,
        )
        items.extend(
            _compact_family(legacy._present_family(row, trust="all"))  # noqa: SLF001
            for row in rows
        )
    return items, len(items), True


def build_snapshot(path: Path = DEFAULT_OUTPUT) -> tuple[dict[str, object], dict[str, object] | None]:
    previous = _load_previous(path)
    attempts = max(1, min(6, int(os.getenv("GAIA_STATIC_SNAPSHOT_DB_ATTEMPTS", "4"))))
    last_error: Exception | None = None
    family_index: list[dict[str, object]] | None = None
    family_index_total = 0
    family_index_complete = False

    for attempt in range(1, attempts + 1):
        try:
            started = time.monotonic()
            print(f"snapshot DB attempt={attempt} connecting", flush=True)
            with legacy.db.connect() as connection:
                print(
                    f"snapshot DB attempt={attempt} connected "
                    f"elapsed={time.monotonic() - started:.3f}s",
                    flush=True,
                )
                family_index, family_index_total, family_index_complete = _indexed_family_index(
                    connection
                )
            print(
                f"snapshot DB attempt={attempt} complete families={family_index_total} "
                f"elapsed={time.monotonic() - started:.3f}s",
                flush=True,
            )
            break
        except (psycopg.Error, OSError, TimeoutError) as error:
            last_error = error
            print(
                f"snapshot DB attempt={attempt} failed type={type(error).__name__}: {error}",
                flush=True,
            )
            if attempt >= attempts:
                raise
            time.sleep(min(8, 2 * attempt))

    if family_index is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("snapshot family export produced no result")

    generated_at = datetime.now(UTC).isoformat()
    health = _snapshot_health(previous, generated_at)
    payload: dict[str, object] = {
        "schema_version": 2,
        "generated_at": generated_at,
        "source_activity_at": _source_activity(previous, health),
        "max_stale_seconds": 86_400,
        "family_index": family_index,
        "family_index_total": family_index_total,
        "family_index_complete": family_index_complete,
        "responses": _responses_from_index(family_index, health),
    }
    return payload, previous


def write_snapshot(path: Path = DEFAULT_OUTPUT) -> Path:
    payload, previous = build_snapshot(path)
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
        description="Export GAIA inventory without coupling publication to crawler health"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(write_snapshot(args.output))


if __name__ == "__main__":
    main()
