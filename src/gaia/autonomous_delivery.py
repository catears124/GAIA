from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from .continuous_runtime_api import ensure_public_feed_current
from .db import Database
from .discord_notify_fast import send_notifications
from .runtime_lead_verification import verify_fresh_leads


async def _drain(database: Database) -> tuple[dict[str, object] | None, str | None]:
    try:
        return await asyncio.to_thread(send_notifications, database), None
    except Exception as error:  # noqa: BLE001 - a later drain gets one independent retry.
        return None, repr(error)


async def run_autonomous_delivery(
    *,
    limit: int = 12,
    concurrency: int = 6,
    timeout_seconds: float = 150.0,
) -> dict[str, Any]:
    """Deliver first, then do bounded verification work, then deliver new commits.

    Nothing expensive is allowed in front of the first webhook drain. In particular,
    this function performs no schema/index DDL and does not call Vercel. That guarantees
    an old verification backlog or slow maintenance query cannot starve already-pending
    Discord notifications.
    """
    database = Database(migrate=False)

    # Highest-priority invariant: committed/projected alerts get a send attempt as soon
    # as the scheduled runner starts. Do this before verification or family rebuilding.
    pre_notifications, pre_notification_error = await _drain(database)

    verification: dict[str, object] | None = None
    verification_error: str | None = None
    try:
        verification = await asyncio.wait_for(
            verify_fresh_leads(
                database,
                limit=max(1, min(int(limit), 32)),
                concurrency=max(1, min(int(concurrency), 12)),
                max_age_days=2,
            ),
            timeout=max(10.0, float(timeout_seconds)),
        )
    except TimeoutError:
        verification_error = f"direct verification exceeded {timeout_seconds:g} seconds"
    except Exception as error:  # noqa: BLE001 - Discord gets its second drain regardless.
        verification_error = repr(error)

    changed = bool(
        verification
        and any(
            int(verification.get(key) or 0) > 0
            for key in (
                "recovered_verified_openings",
                "verified_leads",
                "closed_leads",
            )
        )
    )

    projection: dict[str, Any] | None = None
    projection_error: str | None = None
    try:
        projection = await asyncio.to_thread(
            ensure_public_feed_current,
            database,
            force=changed,
        )
    except Exception as error:  # noqa: BLE001 - final Discord drain must still execute.
        projection_error = repr(error)

    # Drain again so jobs recovered by this very verification pulse are delivered in
    # the same run. This also acts as one independent retry if the first drain failed.
    notifications, notification_error = await _drain(database)

    delivery_succeeded = notification_error is None
    result: dict[str, Any] = {
        "status": "ok" if delivery_succeeded else "partial",
        "pre_notifications": pre_notifications,
        "pre_notification_error": pre_notification_error,
        "verification": verification,
        "verification_error": verification_error,
        "projection": projection,
        "projection_error": projection_error,
        "notifications": notifications,
        "notification_error": notification_error,
    }
    # Verification/projection failures are recoverable next pulse. A failed final drain
    # is not: make the workflow red so transport failures can never masquerade as health.
    if not delivery_succeeded:
        raise RuntimeError(json.dumps(result, sort_keys=True, default=str))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Direct-DB GAIA verification, feed projection, and Discord delivery"
    )
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=float, default=150.0)
    args = parser.parse_args()
    result = asyncio.run(
        run_autonomous_delivery(
            limit=args.limit,
            concurrency=args.concurrency,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
