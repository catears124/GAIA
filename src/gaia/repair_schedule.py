from __future__ import annotations

import json

from .db import Database
from .health import FRESHNESS_FLOOR_SECONDS, FRESHNESS_INTERVAL_MULTIPLIER


def repair_current_schedule(database: Database) -> int:
    """Keep successful empty current boards on their normal crawl cadence.

    The generic worker backs all empty boards off for six hours. That is valid for
    historical sources, but it guarantees current sources become stale under the
    90-minute production freshness contract. Requeue stale empty current boards now
    and cap future empty-board delay at the configured source interval.
    """
    with database.connect() as connection:
        rows = connection.execute(
            """
            UPDATE crawl_targets AS target
            SET next_run_at = CASE
                    WHEN target.last_complete_at IS NULL
                      OR target.last_complete_at < now() - make_interval(
                            secs => GREATEST(target.interval_seconds * %s, %s)
                        )
                    THEN now()
                    ELSE LEAST(
                        target.next_run_at,
                        now() + make_interval(secs => target.interval_seconds)
                    )
                END,
                updated_at = now()
            FROM source_catalog AS catalog
            WHERE catalog.source = target.source
              AND catalog.validated
              AND catalog.scope = 'current'
              AND target.enabled
              AND target.scheduled
              AND target.last_status = 'empty'
              AND target.next_run_at > now() + make_interval(secs => target.interval_seconds)
            RETURNING target.source
            """,
            (FRESHNESS_INTERVAL_MULTIPLIER, FRESHNESS_FLOOR_SECONDS),
        ).fetchall()
    return len(rows)


def main() -> None:
    repaired = repair_current_schedule(Database(migrate=False))
    print(json.dumps({"requeued_empty_current_sources": repaired}, sort_keys=True))


if __name__ == "__main__":
    main()
