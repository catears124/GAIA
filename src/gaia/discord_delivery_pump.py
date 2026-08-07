from __future__ import annotations

import argparse
import json
import time
from typing import Any

from .continuous_runtime_api import ensure_public_feed_current
from .db import Database
from .discord_notify_fast import send_notifications


def run_delivery_pump(
    database: Database | None = None,
    *,
    projection_attempts: int = 5,
    retry_seconds: float = 3.0,
) -> dict[str, Any]:
    """Repair the public projection, then drain Discord from that same read model.

    Candidate/source workers commit postings independently. A concurrent family rebuild
    can briefly make `ensure_public_feed_current()` return `busy=True`, which previously
    allowed a completion-triggered Discord runner to observe zero pending families even
    though newer postings were already committed. This pump gives the projection a few
    bounded retries before selecting alerts, while remaining completely independent of
    Vercel and crawler execution.
    """
    database = database or Database(migrate=False)
    attempts = max(1, min(int(projection_attempts), 10))
    delay = max(0.25, min(float(retry_seconds), 10.0))

    projection: dict[str, Any] | None = None
    projection_error: str | None = None
    projection_attempts_used = 0
    for attempt in range(1, attempts + 1):
        projection_attempts_used = attempt
        try:
            projection = ensure_public_feed_current(database)
            projection_error = None
        except Exception as error:  # noqa: BLE001 - alert drain still gets an attempt.
            projection_error = repr(error)
            projection = None

        if projection is not None and not bool(projection.get("lagging")):
            break
        if attempt < attempts:
            time.sleep(delay)

    notifications = send_notifications(database)
    return {
        "status": "ok",
        "projection": projection,
        "projection_error": projection_error,
        "projection_attempts": projection_attempts_used,
        "notifications": notifications,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair GAIA family projection and drain Discord alerts"
    )
    parser.add_argument("--projection-attempts", type=int, default=5)
    parser.add_argument("--retry-seconds", type=float, default=3.0)
    args = parser.parse_args()
    result = run_delivery_pump(
        projection_attempts=args.projection_attempts,
        retry_seconds=args.retry_seconds,
    )
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
