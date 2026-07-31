from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


BAD_STATUSES = {"broken", "blocked", "truncated", "partial"}


@dataclass(frozen=True)
class SourceAttention:
    source: str
    reason: str
    status: str | None
    last_complete_at: str | None


@dataclass(frozen=True)
class CoverageReport:
    state: str
    description: str
    total: int
    fresh: int
    unhealthy: int
    attention: list[SourceAttention]


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


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _attention_reason(row: dict[str, Any], *, now: datetime) -> str | None:
    if row.get("enabled") is not True:
        return None
    status = str(row.get("crawl_status") or row.get("status") or "").casefold()
    if status in BAD_STATUSES:
        return status
    last_complete = _timestamp(row.get("last_complete_at"))
    if last_complete is None:
        return "never_completed"
    interval = _integer(row.get("interval_seconds"))
    if interval is not None and interval > 0 and last_complete < now - timedelta(seconds=interval * 2):
        lease = _timestamp(row.get("lease_expires_at"))
        return "overdue_running" if lease is not None and lease > now else "overdue"
    return None


def evaluate_coverage(
    health: object,
    coverage: object,
    *,
    now: datetime | None = None,
) -> CoverageReport:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if not isinstance(health, dict) or health.get("stale") is True:
        return CoverageReport("failure", "Health evidence is missing or stale", 0, 0, 0, [])
    inventory = health.get("inventory")
    if not isinstance(inventory, dict):
        return CoverageReport("failure", "Health evidence omitted inventory", 0, 0, 0, [])
    total = _integer(inventory.get("total"))
    fresh = _integer(inventory.get("fresh"))
    unhealthy = _integer(inventory.get("unhealthy"))
    if (
        total is None
        or fresh is None
        or unhealthy is None
        or total <= 0
        or fresh < 0
        or unhealthy < 0
        or fresh > total
        or unhealthy > total
    ):
        return CoverageReport("failure", "Inventory counts are invalid", 0, 0, 0, [])
    if not isinstance(coverage, dict) or not isinstance(coverage.get("sources"), list):
        return CoverageReport(
            "failure", "Coverage evidence omitted source diagnostics", total, fresh, unhealthy, []
        )

    attention: list[SourceAttention] = []
    seen: set[str] = set()
    for raw in coverage["sources"]:
        if not isinstance(raw, dict):
            return CoverageReport(
                "failure", "Coverage evidence contained a non-object source", total, fresh, unhealthy, []
            )
        source = str(raw.get("source") or "").strip()
        if not source or source in seen:
            return CoverageReport(
                "failure", "Coverage evidence contained blank or duplicate sources", total, fresh, unhealthy, []
            )
        seen.add(source)
        reason = _attention_reason(raw, now=current)
        if reason:
            attention.append(
                SourceAttention(
                    source=source,
                    reason=reason,
                    status=(
                        str(raw.get("crawl_status") or raw.get("status"))
                        if raw.get("crawl_status") or raw.get("status")
                        else None
                    ),
                    last_complete_at=(
                        str(raw["last_complete_at"])
                        if raw.get("last_complete_at")
                        else None
                    ),
                )
            )

    attention.sort(key=lambda item: (item.reason, item.source))
    if unhealthy > len(attention):
        return CoverageReport(
            "failure",
            f"Coverage diagnostics omitted {unhealthy - len(attention)} unhealthy configured source",
            total,
            fresh,
            unhealthy,
            attention,
        )
    if health.get("ok") is True and (unhealthy or attention):
        return CoverageReport(
            "failure",
            "Health endpoint reports ok while source diagnostics remain unhealthy",
            total,
            fresh,
            unhealthy,
            attention,
        )
    if unhealthy or attention:
        count = max(unhealthy, len(attention))
        noun = "source" if count == 1 else "sources"
        verb = "needs" if count == 1 else "need"
        return CoverageReport(
            "pending",
            f"Inventory catch-up {fresh}/{total}; {count} {noun} {verb} attention",
            total,
            fresh,
            unhealthy,
            attention,
        )
    if fresh != total:
        return CoverageReport(
            "failure",
            f"Inventory counts disagree: fresh={fresh} total={total} unhealthy={unhealthy}",
            total,
            fresh,
            unhealthy,
            attention,
        )
    return CoverageReport(
        "success",
        f"All {total} configured sources are fresh",
        total,
        fresh,
        unhealthy,
        attention,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("health", type=Path)
    parser.add_argument("coverage", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        health = json.loads(args.health.read_text(encoding="utf-8"))
        coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        report = CoverageReport(
            "failure", f"Could not parse production evidence: {error}", 0, 0, 0, []
        )
    else:
        report = evaluate_coverage(health, coverage)
    payload = json.dumps(asdict(report), separators=(",", ":"), sort_keys=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(args.output)
    print(payload)


if __name__ == "__main__":
    main()
