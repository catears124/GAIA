from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Probe:
    status: int
    body: str


@dataclass(frozen=True)
class SmokeResult:
    state: str
    description: str
    snapshot_usable: bool
    statuses: dict[str, int]


def _json(body: str) -> Any | None:
    try:
        return json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def snapshot_is_usable(probe: Probe, *, now: datetime | None = None) -> bool:
    payload = _json(probe.body)
    if probe.status != 200 or not isinstance(payload, dict):
        return False
    index = payload.get("family_index")
    total = payload.get("family_index_total")
    generated = _timestamp(payload.get("generated_at"))
    max_stale_seconds = payload.get("max_stale_seconds", 86_400)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if (
        generated is None
        or not isinstance(max_stale_seconds, int)
        or isinstance(max_stale_seconds, bool)
        or not 60 <= max_stale_seconds <= 604_800
        or generated > current + timedelta(minutes=5)
        or current - generated > timedelta(seconds=max_stale_seconds)
        or not isinstance(index, list)
        or not index
        or payload.get("family_index_complete") is not True
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total != len(index)
    ):
        return False
    keys: list[str] = []
    for item in index:
        if not isinstance(item, dict):
            return False
        key = item.get("family_key")
        if not isinstance(key, str) or not key.strip():
            return False
        keys.append(key.strip())
    return len(set(keys)) == len(keys)


def _valid_database_outage(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    inventory = payload.get("inventory")
    progress = payload.get("progress")
    return (
        payload.get("ok") is False
        and payload.get("stale") is True
        and payload.get("reason") == "database_unavailable"
        and isinstance(inventory, dict)
        and inventory.get("healthy") is False
        and inventory.get("total") == 0
        and isinstance(progress, dict)
        and progress.get("stage") == "database-recovery"
    )


def _inventory_counts(inventory: dict[str, object]) -> tuple[int, int] | None:
    total = inventory.get("total")
    fresh = inventory.get("fresh")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or not isinstance(fresh, int)
        or isinstance(fresh, bool)
        or total <= 0
        or fresh < 0
        or fresh > total
    ):
        return None
    return fresh, total


def evaluate(probes: dict[str, Probe]) -> SmokeResult:
    required = {"index", "emergency", "controller", "snapshot", "health", "stats", "families"}
    missing = sorted(required - probes.keys())
    statuses = {name: probe.status for name, probe in probes.items()}
    if missing:
        return SmokeResult(
            "failure", f"Smoke evidence missing: {', '.join(missing)}", False, statuses
        )

    snapshot_usable = snapshot_is_usable(probes["snapshot"])
    index = probes["index"]
    if index.status != 200:
        return SmokeResult(
            "failure",
            f"Deployed UI unavailable (HTTP {index.status})",
            snapshot_usable,
            statuses,
        )
    if "emergency-outage.js" not in index.body or "api-resilience.js" not in index.body:
        return SmokeResult(
            "failure", "Deployed UI missing resilience runtimes", snapshot_usable, statuses
        )

    emergency = probes["emergency"]
    if emergency.status != 200 or "MAX_EMERGENCY_AGE_MS" not in emergency.body:
        return SmokeResult(
            "failure", "Emergency inventory runtime missing or invalid", snapshot_usable, statuses
        )

    controller = probes["controller"]
    if (
        controller.status != 200
        or "liveHealthProbe" not in controller.body
        or "XMLHttpRequest" not in controller.body
    ):
        return SmokeResult(
            "failure",
            "Automatic outage recovery controller missing or stale",
            snapshot_usable,
            statuses,
        )

    health = probes["health"]
    health_payload = _json(health.body)
    degraded_inventory: tuple[int, int] | None = None
    if health.status == 200:
        if (
            not isinstance(health_payload, dict)
            or not isinstance(health_payload.get("inventory"), dict)
            or not isinstance(health_payload.get("ok"), bool)
        ):
            return SmokeResult(
                "failure", "Health API returned an invalid contract", snapshot_usable, statuses
            )
        inventory = health_payload["inventory"]
        if not isinstance(inventory.get("healthy"), bool):
            return SmokeResult(
                "failure", "Health API omitted inventory health state", snapshot_usable, statuses
            )
        counts = _inventory_counts(inventory)
        if counts is None:
            return SmokeResult(
                "failure",
                "Health API returned invalid or empty inventory counts",
                snapshot_usable,
                statuses,
            )
        if health_payload["ok"] is True and inventory["healthy"] is not True:
            return SmokeResult(
                "failure",
                "Health API dishonestly reports ok with unhealthy inventory",
                snapshot_usable,
                statuses,
            )
        if health_payload["ok"] is False and inventory["healthy"] is True:
            return SmokeResult(
                "failure",
                "Health API reports contradictory inventory state",
                snapshot_usable,
                statuses,
            )
        if health_payload.get("stale") is True:
            return SmokeResult(
                "failure", "Health API returned stale data as a live response", snapshot_usable, statuses
            )
        if health_payload["ok"] is False:
            degraded_inventory = counts
    elif health.status == 503:
        if not _valid_database_outage(health_payload):
            return SmokeResult(
                "failure",
                "Health API returned an invalid database-outage contract",
                snapshot_usable,
                statuses,
            )
        if snapshot_usable:
            return SmokeResult(
                "pending",
                "Database recovery active; deployed offline inventory is usable",
                True,
                statuses,
            )
        return SmokeResult(
            "failure",
            "Database recovery active and first-visit inventory snapshot is unusable",
            False,
            statuses,
        )
    elif health.status == 0:
        return SmokeResult(
            "failure", "Health API timed out or could not be reached", snapshot_usable, statuses
        )
    elif 500 <= health.status <= 599:
        return SmokeResult(
            "failure",
            f"Health API returned an unclassified server failure (HTTP {health.status})",
            snapshot_usable,
            statuses,
        )
    else:
        return SmokeResult(
            "failure",
            f"Health API unavailable (HTTP {health.status})",
            snapshot_usable,
            statuses,
        )

    stats_payload = _json(probes["stats"].body)
    if probes["stats"].status != 200 or not isinstance(stats_payload, dict):
        return SmokeResult(
            "failure",
            f"Stats API contract failed (HTTP {probes['stats'].status})",
            snapshot_usable,
            statuses,
        )

    families_payload = _json(probes["families"].body)
    if (
        probes["families"].status != 200
        or not isinstance(families_payload, dict)
        or not isinstance(families_payload.get("items"), list)
        or not isinstance(families_payload.get("total"), int)
        or isinstance(families_payload.get("total"), bool)
        or families_payload["total"] < len(families_payload["items"])
    ):
        return SmokeResult(
            "failure",
            f"Families API contract failed (HTTP {probes['families'].status})",
            snapshot_usable,
            statuses,
        )

    if degraded_inventory is not None:
        fresh, total = degraded_inventory
        return SmokeResult(
            "pending",
            f"Production reachable; inventory catch-up {fresh}/{total} fresh",
            snapshot_usable,
            statuses,
        )
    return SmokeResult(
        "success", "Production UI and APIs passed black-box smoke checks", snapshot_usable, statuses
    )


def load_probe(directory: Path, name: str) -> Probe:
    try:
        status = int((directory / f"{name}.status").read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        status = 0
    try:
        body = (directory / f"{name}.body").read_text(encoding="utf-8")
    except FileNotFoundError:
        body = ""
    return Probe(status=status, body=body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    names = ("index", "emergency", "controller", "snapshot", "health", "stats", "families")
    result = evaluate({name: load_probe(args.directory, name) for name in names})
    payload = json.dumps(asdict(result), separators=(",", ":"), sort_keys=True)
    if args.output:
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(args.output)
    print(payload)


if __name__ == "__main__":
    main()
