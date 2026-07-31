from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
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


def snapshot_is_usable(probe: Probe) -> bool:
    payload = _json(probe.body)
    if probe.status != 200 or not isinstance(payload, dict):
        return False
    index = payload.get("family_index")
    total = payload.get("family_index_total")
    generated = payload.get("generated_at")
    return (
        isinstance(generated, str)
        and bool(generated.strip())
        and isinstance(index, list)
        and len(index) > 0
        and payload.get("family_index_complete") is True
        and isinstance(total, int)
        and not isinstance(total, bool)
        and total == len(index)
        and len({str(item.get("family_key", "")) for item in index if isinstance(item, dict)})
        == len(index)
    )


def evaluate(probes: dict[str, Probe]) -> SmokeResult:
    required = {"index", "emergency", "controller", "snapshot", "health", "stats", "families"}
    missing = sorted(required - probes.keys())
    statuses = {name: probe.status for name, probe in probes.items()}
    if missing:
        return SmokeResult("failure", f"Smoke evidence missing: {', '.join(missing)}", False, statuses)

    snapshot_usable = snapshot_is_usable(probes["snapshot"])
    index = probes["index"]
    if index.status != 200:
        return SmokeResult("failure", f"Deployed UI unavailable (HTTP {index.status})", snapshot_usable, statuses)
    if "emergency-outage.js" not in index.body or "api-resilience.js" not in index.body:
        return SmokeResult("failure", "Deployed UI missing resilience runtimes", snapshot_usable, statuses)

    emergency = probes["emergency"]
    if emergency.status != 200 or "MAX_EMERGENCY_AGE_MS" not in emergency.body:
        return SmokeResult("failure", "Emergency inventory runtime missing or invalid", snapshot_usable, statuses)

    controller = probes["controller"]
    if controller.status != 200 or "liveHealthProbe" not in controller.body or "XMLHttpRequest" not in controller.body:
        return SmokeResult("failure", "Automatic outage recovery controller missing or stale", snapshot_usable, statuses)

    health = probes["health"]
    health_payload = _json(health.body)
    if health.status == 200:
        if not isinstance(health_payload, dict) or not isinstance(health_payload.get("inventory"), dict) or not isinstance(health_payload.get("ok"), bool):
            return SmokeResult("failure", "Health API returned an invalid contract", snapshot_usable, statuses)
        if health_payload["ok"] is True and health_payload["inventory"].get("healthy") is not True:
            return SmokeResult("failure", "Health API dishonestly reports ok with unhealthy inventory", snapshot_usable, statuses)
        if health_payload.get("stale") is True:
            return SmokeResult("failure", "Health API returned stale data as a live response", snapshot_usable, statuses)
    elif 500 <= health.status <= 599:
        if snapshot_usable:
            return SmokeResult("pending", "Database/API recovery active; deployed offline inventory is usable", True, statuses)
        return SmokeResult("failure", "Database/API offline and first-visit inventory snapshot is unusable", False, statuses)
    else:
        return SmokeResult("failure", f"Health API unavailable (HTTP {health.status})", snapshot_usable, statuses)

    stats_payload = _json(probes["stats"].body)
    if probes["stats"].status != 200 or not isinstance(stats_payload, dict):
        return SmokeResult("failure", f"Stats API contract failed (HTTP {probes['stats'].status})", snapshot_usable, statuses)

    families_payload = _json(probes["families"].body)
    if (
        probes["families"].status != 200
        or not isinstance(families_payload, dict)
        or not isinstance(families_payload.get("items"), list)
        or not isinstance(families_payload.get("total"), int)
        or isinstance(families_payload.get("total"), bool)
        or families_payload["total"] < len(families_payload["items"])
    ):
        return SmokeResult("failure", f"Families API contract failed (HTTP {probes['families'].status})", snapshot_usable, statuses)

    return SmokeResult("success", "Production UI and APIs passed black-box smoke checks", snapshot_usable, statuses)


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
