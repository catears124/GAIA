from __future__ import annotations

import argparse
import json
from typing import Any

from .db import Database
from .discord_notify import TARGET_MATCHES
from .quality import TECH_CATEGORIES


def diagnostic(database: Database | None = None, *, limit: int = 30) -> dict[str, Any]:
    """Explain Discord eligibility for the newest projected families.

    This intentionally reads only the small `families`, delivery, claim, and channel
    tables. It never touches raw posting history, so it is safe to run while production
    crawlers are busy.
    """
    database = database or Database(migrate=False)
    bounded = max(1, min(int(limit), 100))
    with database.connect() as connection:
        families = connection.execute(
            """
            SELECT
                family.family_key,
                family.company,
                family.title,
                family.category,
                family.target_match,
                family.first_detected_at,
                family.last_verified_at,
                family.direct_openings,
                family.backstop_openings,
                family.locations,
                EXISTS (
                    SELECT 1
                    FROM discord_notification_deliveries AS delivery
                    WHERE delivery.channel='verified'
                      AND delivery.family_key=family.family_key
                ) AS verified_has_delivery,
                (
                    SELECT delivery.disposition
                    FROM discord_notification_deliveries AS delivery
                    WHERE delivery.channel='verified'
                      AND delivery.family_key=family.family_key
                ) AS verified_disposition,
                (
                    SELECT delivery.discord_message_id
                    FROM discord_notification_deliveries AS delivery
                    WHERE delivery.channel='verified'
                      AND delivery.family_key=family.family_key
                ) AS verified_message_id,
                (
                    SELECT delivery.delivered_at
                    FROM discord_notification_deliveries AS delivery
                    WHERE delivery.channel='verified'
                      AND delivery.family_key=family.family_key
                ) AS verified_delivered_at,
                EXISTS (
                    SELECT 1
                    FROM discord_notification_deliveries AS delivery
                    WHERE delivery.channel='leads'
                      AND delivery.family_key=family.family_key
                ) AS leads_has_delivery,
                (
                    SELECT delivery.disposition
                    FROM discord_notification_deliveries AS delivery
                    WHERE delivery.channel='leads'
                      AND delivery.family_key=family.family_key
                ) AS leads_disposition,
                EXISTS (
                    SELECT 1
                    FROM discord_notification_claims AS claim
                    WHERE claim.channel='verified'
                      AND claim.family_key=family.family_key
                ) AS verified_claimed,
                EXISTS (
                    SELECT 1
                    FROM discord_notification_claims AS claim
                    WHERE claim.channel='leads'
                      AND claim.family_key=family.family_key
                ) AS leads_claimed
            FROM families AS family
            ORDER BY family.first_detected_at DESC, family.family_key
            LIMIT %s
            """,
            (bounded,),
        ).fetchall()
        channels = connection.execute(
            """
            SELECT channel, initialized_at, updated_at,
                   (SELECT COUNT(*) FROM discord_notification_deliveries d
                    WHERE d.channel=c.channel AND d.disposition='sent') AS sent_total,
                   (SELECT COUNT(*) FROM discord_notification_deliveries d
                    WHERE d.channel=c.channel AND d.disposition='suppressed') AS suppressed_total,
                   (SELECT MAX(delivered_at) FROM discord_notification_deliveries d
                    WHERE d.channel=c.channel AND d.disposition='sent') AS last_sent_at
            FROM discord_notification_channels AS c
            ORDER BY channel
            """
        ).fetchall()

    rows: list[dict[str, Any]] = []
    targets = set(TARGET_MATCHES)
    categories = set(TECH_CATEGORIES)
    for raw in families:
        row = dict(raw)
        eligible_base = (
            str(row.get("target_match")) in targets
            and str(row.get("category")) in categories
        )
        row["verified_eligible"] = bool(
            eligible_base
            and int(row.get("direct_openings") or 0) > 0
        )
        row["lead_eligible"] = bool(
            eligible_base
            and int(row.get("direct_openings") or 0) == 0
            and int(row.get("backstop_openings") or 0) > 0
        )
        rows.append(row)
    return {
        "channels": [dict(row) for row in channels],
        "newest_families": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect GAIA Discord delivery ledger")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(diagnostic(limit=args.limit), sort_keys=True, default=str))


if __name__ == "__main__":
    main()
