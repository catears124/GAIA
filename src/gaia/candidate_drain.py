from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from .continuous_runtime_api import ensure_public_feed_current
from .discord_notify_fast import send_notifications
from .dynamic_market_discovery import _client
from .fast_dynamic_ingest import _claim_balanced_candidates, _schedule_promoted_sources
from .live_inventory import InventoryWorker, LiveDatabase


async def drain_candidates(
    *,
    limit: int = 32,
    concurrency: int = 8,
) -> dict[str, Any]:
    """Probe a bounded provider-balanced slice of the persistent source queue."""
    database = LiveDatabase(migrate=False)
    worker = InventoryWorker(database, concurrency=max(1, int(concurrency)))
    claimed = _claim_balanced_candidates(worker, limit=max(1, int(limit)))

    outcomes: list[bool] = []
    async with _client(max(1, int(concurrency))) as client:
        if claimed:
            outcomes = list(
                await asyncio.gather(
                    *(worker._probe_candidate(client, target) for target in claimed)
                )
            )

    promoted_targets = [
        target for target, promoted in zip(claimed, outcomes, strict=False) if promoted
    ]
    scheduled = _schedule_promoted_sources(worker, promoted_targets)

    projection: dict[str, Any] | None = None
    projection_error: str | None = None
    if promoted_targets:
        try:
            projection = await asyncio.to_thread(
                ensure_public_feed_current,
                database,
                force=True,
            )
        except Exception as error:  # noqa: BLE001 - committed jobs survive for next repair.
            projection_error = repr(error)

    notifications: dict[str, object] | None = None
    notification_error: str | None = None
    try:
        notifications = await asyncio.to_thread(send_notifications, database)
    except Exception as error:  # noqa: BLE001 - expose transport failure without losing probes.
        notification_error = repr(error)

    result: dict[str, Any] = {
        "status": "ok" if notification_error is None else "partial",
        "candidate_sources_probed": len(claimed),
        "candidate_probe_kinds": sorted({target.kind for target in claimed}),
        "candidate_sources_promoted": len(promoted_targets),
        "promoted_sources": [target.source for target in promoted_targets],
        "catalog_sources_scheduled": scheduled,
        "feed_projection": projection,
        "feed_projection_error": projection_error,
        "notifications": notifications,
        "notification_error": notification_error,
    }
    if notification_error is not None:
        raise RuntimeError(json.dumps(result, sort_keys=True, default=str))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drain GAIA's persistent market source candidate queue"
    )
    parser.add_argument("--limit", type=int, default=32)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    result = asyncio.run(
        drain_candidates(limit=args.limit, concurrency=args.concurrency)
    )
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
