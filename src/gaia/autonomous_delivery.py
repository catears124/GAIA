from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from .continuous_runtime_api import ensure_public_feed_current
from .db import Database
from .discord_notify_fast import send_notifications
from .runtime_lead_verification import verify_fresh_leads

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_postings_runtime_lead_due
    ON postings (first_seen_at DESC, link_checked_at, posting_key)
    WHERE active
      AND source_mode IN ('registry','external-index','verification-lead')
      AND target_match IN ('exact','year_confirmed','source_confirmed')
      AND link_status NOT IN ('closed','invalid','verified');
CREATE INDEX IF NOT EXISTS idx_families_discord_verified_due
    ON families (first_detected_at, family_key)
    WHERE direct_openings > 0;
CREATE INDEX IF NOT EXISTS idx_families_discord_lead_due
    ON families (first_detected_at, family_key)
    WHERE direct_openings = 0 AND backstop_openings > 0;
"""


def ensure_delivery_indexes(database: Database) -> None:
    """Install small targeted indexes used by autonomous verification/delivery."""
    with database.connect() as connection:
        connection.execute(_INDEXES)


async def run_autonomous_delivery(
    *,
    limit: int = 12,
    concurrency: int = 6,
    timeout_seconds: float = 150.0,
) -> dict[str, Any]:
    database = Database(migrate=False)
    await asyncio.to_thread(ensure_delivery_indexes, database)

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
    except Exception as error:  # noqa: BLE001 - Discord must still drain on verifier failure.
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
    except Exception as error:  # noqa: BLE001 - notifications can still use last projection.
        projection_error = repr(error)

    notifications: dict[str, object] | None = None
    notification_error: str | None = None
    try:
        notifications = await asyncio.to_thread(send_notifications, database)
    except Exception as error:  # noqa: BLE001 - expose exact failure to workflow evidence.
        notification_error = repr(error)

    result: dict[str, Any] = {
        "status": "ok" if notification_error is None else "partial",
        "verification": verification,
        "verification_error": verification_error,
        "projection": projection,
        "projection_error": projection_error,
        "notifications": notifications,
        "notification_error": notification_error,
    }
    # A verifier timeout is recoverable: the next five-minute pulse retries. A notifier
    # failure is not recoverable from the user's perspective and must make the job red.
    if notification_error is not None:
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
