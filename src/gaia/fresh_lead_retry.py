from __future__ import annotations

from datetime import UTC, datetime

from .lead_promotion import _BACKSTOP_MODES, promote_leads


async def retry_fresh_leads(
    *,
    limit: int,
    concurrency: int,
    hours: int,
    retry_after_minutes: int = 10,
) -> dict[str, object]:
    """Retry only recent leads that previously failed for transient reasons.

    Old or never-actionable inventory is deliberately excluded. This lane targets jobs
    discovered during the last 24 hours whose employer page was blocked, timed out, or
    returned unstructured content on an earlier attempt. A row-locking reset lets the
    normal lead promoter claim them safely without duplicating concurrent workers.
    """
    from .live_inventory import LiveDatabase

    bounded_limit = max(1, min(int(limit), 64))
    bounded_retry = max(5, min(int(retry_after_minutes), 180))
    database = LiveDatabase(migrate=False)
    started_at = datetime.now(UTC)

    with database.connect() as connection:
        reset_rows = connection.execute(
            """
            WITH due AS (
                SELECT posting_key
                FROM postings
                WHERE active
                  AND source_mode = ANY(%s)
                  AND target_match IN ('exact','year_confirmed','source_confirmed')
                  AND first_seen_at >= now() - interval '24 hours'
                  AND link_status IN ('blocked','error','unverified','checking')
                  AND link_checked_at <= now() - (%s * interval '1 minute')
                ORDER BY first_seen_at DESC, link_checked_at, posting_key
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE postings AS posting
            SET link_checked_at=NULL, link_status='unchecked'
            FROM due
            WHERE posting.posting_key=due.posting_key
            RETURNING posting.posting_key,posting.company,posting.title,
              posting.canonical_apply_url
            """,
            (list(_BACKSTOP_MODES), bounded_retry, bounded_limit),
        ).fetchall()

    result = await promote_leads(
        limit=bounded_limit,
        concurrency=concurrency,
        hours=hours,
        max_age_days=1,
    )

    with database.connect() as connection:
        recovered = connection.execute(
            """
            SELECT DISTINCT ON (company,title,canonical_apply_url)
              company,title,canonical_apply_url,source,first_seen_at,last_seen_at
            FROM postings
            WHERE active
              AND source_mode='verification'
              AND target_match IN ('exact','year_confirmed','source_confirmed')
              AND last_seen_at >= %s
            ORDER BY company,title,canonical_apply_url,last_seen_at DESC
            LIMIT 64
            """,
            (started_at,),
        ).fetchall()

    result["fresh_retry_reset"] = len(reset_rows)
    result["fresh_retry_after_minutes"] = bounded_retry
    result["fresh_retry_candidates"] = [
        {
            "company": str(row["company"]),
            "title": str(row["title"]),
            "canonical_apply_url": str(row["canonical_apply_url"]),
        }
        for row in reset_rows
    ]
    result["recovered_samples"] = [
        {
            "company": str(row["company"]),
            "title": str(row["title"]),
            "canonical_apply_url": str(row["canonical_apply_url"]),
            "source": str(row["source"]),
            "first_seen_at": row["first_seen_at"].isoformat(),
            "last_seen_at": row["last_seen_at"].isoformat(),
        }
        for row in recovered
    ]
    result["recovered_employers"] = sorted(
        {str(row["company"]) for row in recovered}
    )
    return result
