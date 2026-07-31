from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_RESPONSES = {"/api/health", "/api/stats", "/api/facets", "/api/families"}


@dataclass(frozen=True)
class SnapshotReport:
    roles: int
    expected_roles: int
    age_hours: float
    size_mb: float


def validate_snapshot_payload(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_hours: float = 168,
    size_bytes: int | None = None,
    max_size_mb: float = 15,
) -> SnapshotReport:
    responses = payload.get("responses")
    if not isinstance(responses, dict):
        raise ValueError("snapshot responses must be an object")
    missing = REQUIRED_RESPONSES - set(responses)

    index = payload.get("family_index")
    expected = int(payload.get("family_index_total") or 0)
    if missing or not isinstance(index, list) or not index or expected <= 0:
        count = len(index) if isinstance(index, list) else "invalid"
        raise ValueError(
            f"no usable snapshot: missing={sorted(missing)} index={count} expected={expected}"
        )
    if payload.get("family_index_complete") is not True or len(index) < expected:
        raise ValueError(f"offline index incomplete: exported={len(index)} expected={expected}")

    keys: list[str] = []
    for item in index:
        if not isinstance(item, dict):
            raise ValueError("offline index contains a non-object role")
        keys.append(str(item.get("family_key") or ""))
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("offline index contains missing or duplicate keys")

    generated = payload.get("generated_at")
    try:
        generated_at = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("snapshot generated_at is missing or invalid") from exc
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    age_hours = (current.astimezone(UTC) - generated_at.astimezone(UTC)).total_seconds() / 3600
    if age_hours < -1:
        raise ValueError(f"snapshot timestamp is in the future: {age_hours:.1f} hours")
    if age_hours > max_age_hours:
        raise ValueError(f"snapshot is too old: {age_hours:.1f} hours")

    actual_size = int(size_bytes or 0)
    size_mb = actual_size / 1_000_000
    if actual_size and size_mb > max_size_mb:
        raise ValueError(f"snapshot is unexpectedly large: {size_mb:.2f} MB")

    return SnapshotReport(
        roles=len(index), expected_roles=expected, age_hours=max(0.0, age_hours), size_mb=size_mb
    )


def validate_snapshot_file(
    path: Path,
    *,
    now: datetime | None = None,
    max_age_hours: float = 168,
    max_size_mb: float = 15,
) -> SnapshotReport:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"snapshot is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("snapshot root must be an object")
    return validate_snapshot_payload(
        payload,
        now=now,
        max_age_hours=max_age_hours,
        size_bytes=path.stat().st_size,
        max_size_mb=max_size_mb,
    )
