from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MAX_PUBLIC_SNAPSHOT_AGE = timedelta(minutes=45)


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
        or current - generated
        > min(timedelta(seconds=max_stale_seconds), MAX_PUBLIC_SNAPSHOT_AGE)
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


def _validate_resilience_assets(probes: dict[str, Probe]) -> str | None:
    index = probes["index"]
    if index.status != 200:
        return f"Deployed UI unavailable (HTTP {index.status})"
    required_scripts = (
        "remote-snapshot.js?v=1.0.1",
        "api-resilience.js?v=2.0.0",
        "emergency-outage.js?v=2.0.0",
        "outage-controller.js?v=1.2.1",
    )
    missing_scripts = [script for script in required_scripts if script not in index.body]
    if missing_scripts:
        return f"Deployed UI missing resilience assets: {', '.join(missing_scripts)}"

    remote = probes["remote"]
    if (
        remote.status != 200
        or "raw.githubusercontent.com/catears124/GAIA/snapshot-data" not in remote.body
        or 'cache: "no-store"' not in remote.body
        or 'mode: "cors"' not in remote.body
    ):
        return "Remote snapshot transport missing or stale"

    resilience = probes["resilience"]
    if (
        resilience.status != 200
        or "window.fetch = async function resilientFetch" not in resilience.body
        or "staticSnapshotResponse" not in resilience.body
        or "cachedResponse" not in resilience.body
        or "gaia:stale-data" not in resilience.body
    ):
        return "Primary API resilience runtime missing or stale"

    emergency = probes["emergency"]
    if (
        emergency.status != 200
        or "MAX_EMERGENCY_AGE_MS = 0" not in emergency.body
        or "retireLegacyState" not in emergency.body
        or "window.fetch =" in emergency.body
        or "localStorage" in emergency.body
        or "durable device backup" in emergency.body
    ):
        return "Legacy durable-cache runtime is active or invalid"

    controller = probes["controller"]
    if (
        controller.status != 200
        or "liveHealthProbe" not in controller.body
        or "XMLHttpRequest" not in controller.body
    ):
        return "Automatic outage recovery controller missing or stale"
    return None


def evaluate(probes: dict[str, Probe]) -> SmokeResult:
    required = {
        "index",
        "remote",
        "resilience",
        "emergency",
        "controller",
        "snapshot",
        "health",
        "stats",
        "families",
    }
    missing = sorted(required - probes.keys())
    statuses = {name: probe.status for name, probe in probes.items()}
    if missing:
        return SmokeResult(
            "failure", f"Smoke evidence missing: {', '.join(missing)}", False, statuses
        )

    snapshot_usable = snapshot_is_usable(probes["snapshot"])
    asset_error = _validate_resilience_assets(probes)
    if asset_error:
        return SmokeResult("failure", asset_error, snapshot_usable, statuses)

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
        if not snapshot_usable:
            return SmokeResult(
                "failure",
                "Published inventory snapshot is missing, invalid, or older than 45 minutes",
                False,
                statuses,
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
                "Database recovery active; published inventory snapshot is usable",
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
        "success",
        "Production UI, resilience chain, snapshot, and APIs passed black-box checks",
        snapshot_usable,
        statuses,
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
    names = (
        "index",
        "remote",
        "resilience",
        "emergency",
        "controller",
        "snapshot",
        "health",
        "stats",
        "families",
    )
    result = evaluate({name: load_probe(args.directory, name) for name in names})
    payload = json.dumps(asdict(result), separators=(",", ":"), sort_keys=True)
    if args.output:
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(args.output)
    print(payload)


if __name__ == "__main__":
    main()
