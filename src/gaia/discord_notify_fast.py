from __future__ import annotations

import argparse
import json
from typing import Any

from . import discord_notify as legacy
from .db import Database
from .quality import TECH_CATEGORIES


def _pending(
    connection: Any,
    channel: legacy.Channel,
    limit: int,
    *,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Select notification candidates from `families`, never the raw postings table.

    The legacy query grouped and window-ranked the entire active postings table for each
    channel on every two-second watcher iteration. Under production write pressure that
    query exceeded the 120-second PostgreSQL statement timeout. `families` already is the
    exact deduplicated read model needed for notification eligibility and normally has
    only hundreds of rows, so selection remains bounded regardless of posting history.
    """

    if channel.name == "verified":
        state_clause = "family.direct_openings > 0"
        modes = list(legacy.VERIFIED_MODES)
    else:
        state_clause = "family.direct_openings = 0 AND family.backstop_openings > 0"
        modes = list(legacy.LEAD_MODES)

    source_clause = ""
    params: list[object] = [
        list(legacy.TARGET_MATCHES),
        list(TECH_CATEGORIES),
        channel.name,
    ]
    if source:
        source_clause = "AND family.openings @> %s::jsonb"
        params.append(json.dumps([{"source": source}], separators=(",", ":")))
    params.extend([limit, modes])

    rows = connection.execute(
        f"""
        WITH candidate AS MATERIALIZED (
            SELECT
                family.family_key,
                family.company,
                family.title,
                family.locations,
                family.category,
                family.first_detected_at,
                family.openings
            FROM families AS family
            WHERE family.target_match = ANY(%s)
              AND family.category = ANY(%s)
              AND {state_clause}
              AND NOT EXISTS (
                  SELECT 1
                  FROM discord_notification_deliveries AS delivered
                  WHERE delivered.channel=%s
                    AND delivered.family_key=family.family_key
              )
              {source_clause}
            ORDER BY family.first_detected_at, family.family_key
            LIMIT %s
        )
        SELECT
            candidate.family_key,
            candidate.company,
            candidate.title,
            candidate.locations,
            candidate.category,
            candidate.first_detected_at,
            selected.opening->>'apply_url' AS apply_url,
            selected.opening->>'source' AS source,
            NULLIF(selected.opening->>'posted_at', '') AS posted_at
        FROM candidate
        JOIN LATERAL (
            SELECT opening
            FROM jsonb_array_elements(candidate.openings) AS opening
            WHERE opening->>'source_mode' = ANY(%s)
            ORDER BY
                NULLIF(opening->>'posted_at', '') DESC NULLS LAST,
                NULLIF(opening->>'first_detected_at', '') DESC NULLS LAST,
                opening->>'apply_url'
            LIMIT 1
        ) AS selected ON TRUE
        ORDER BY candidate.first_detected_at, candidate.family_key
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def send_notifications(
    database: Database | None = None,
    *,
    source: str | None = None,
) -> dict[str, object]:
    # The legacy sender owns webhook retries, claims, delivery recording, suppression,
    # and exact payload formatting. Replace only the unbounded candidate query.
    legacy._pending = _pending  # type: ignore[attr-defined]
    return legacy.send_notifications(database, source=source)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drain GAIA Discord notifications using the bounded family read model"
    )
    parser.add_argument("--source", default=None)
    args = parser.parse_args()
    result = send_notifications(source=args.source)
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
