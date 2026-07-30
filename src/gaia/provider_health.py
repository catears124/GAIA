from __future__ import annotations

import json

from .db import Database
from .db_base import iso
from .health import BAD_STATUSES, FRESHNESS_FLOOR_SECONDS, FRESHNESS_INTERVAL_MULTIPLIER


def provider_health(database: Database) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            WITH current_targets AS (
                SELECT
                    catalog.kind,
                    target.*,
                    GREATEST(target.interval_seconds * %s, %s) AS freshness_seconds
                FROM crawl_targets AS target
                JOIN source_catalog AS catalog USING(source)
                WHERE target.enabled
                  AND target.scheduled
                  AND catalog.validated
                  AND catalog.scope='current'
            )
            SELECT
                kind,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE lease_expires_at > now()) AS running,
                COUNT(*) FILTER (WHERE next_run_at <= now()) AS due,
                COUNT(*) FILTER (
                    WHERE last_complete_at IS NOT NULL
                      AND last_complete_at >= now() - make_interval(secs => freshness_seconds)
                      AND last_status <> ALL(%s)
                ) AS fresh,
                COUNT(*) FILTER (
                    WHERE last_complete_at IS NULL
                       OR last_complete_at < now() - make_interval(secs => freshness_seconds)
                       OR last_status = ANY(%s)
                ) AS unhealthy,
                COUNT(*) FILTER (WHERE last_status = ANY(%s)) AS degraded,
                MAX(last_finished_at) AS latest_activity_at,
                MIN(last_complete_at) FILTER (WHERE last_complete_at IS NOT NULL)
                    AS coverage_watermark
            FROM current_targets
            GROUP BY kind
            ORDER BY unhealthy DESC, due DESC, kind
            """,
            (
                FRESHNESS_INTERVAL_MULTIPLIER,
                FRESHNESS_FLOOR_SECONDS,
                list(BAD_STATUSES),
                list(BAD_STATUSES),
                list(BAD_STATUSES),
            ),
        ).fetchall()

    result: list[dict[str, object]] = []
    for row in rows:
        total = int(row["total"] or 0)
        due = int(row["due"] or 0)
        fresh = int(row["fresh"] or 0)
        result.append(
            {
                "kind": str(row["kind"]),
                "total": total,
                "running": int(row["running"] or 0),
                "due": due,
                "due_percent": round(100 * due / total, 1) if total else 0.0,
                "fresh": fresh,
                "unhealthy": int(row["unhealthy"] or 0),
                "degraded": int(row["degraded"] or 0),
                "fresh_percent": round(100 * fresh / total, 1) if total else 0.0,
                "latest_activity_at": iso(row["latest_activity_at"]),
                "coverage_watermark": iso(row["coverage_watermark"]),
            }
        )
    return result


def main() -> None:
    print(json.dumps(provider_health(Database(migrate=False)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
