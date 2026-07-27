from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import httpx

from .db import Database
from .page_verification import page_is_closed

TARGET_MATCHES = ("exact", "year_confirmed", "source_confirmed")
ACCEPTED_STATUSES = {200, 401, 403, 406, 429}
CLOSED_STATUSES = {404, 410}


@dataclass(slots=True)
class LinkValidationSummary:
    checked: int = 0
    active: int = 0
    protected: int = 0
    closed: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


async def validate_application_links(
    db: Database,
    client: httpx.AsyncClient,
    *,
    limit: int = 500,
    concurrency: int = 20,
) -> LinkValidationSummary:
    if limit <= 0:
        return LinkValidationSummary()
    placeholders = ",".join("?" for _ in TARGET_MATCHES)
    with db.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT canonical_apply_url, MIN(link_checked_at) AS last_checked
            FROM postings
            WHERE active=1
              AND source_mode='direct'
              AND target_match IN ({placeholders})
            GROUP BY canonical_apply_url
            ORDER BY last_checked IS NOT NULL, last_checked
            LIMIT ?
            """,
            (*TARGET_MATCHES, limit),
        ).fetchall()

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def check(url: str) -> tuple[str, int | None, str, str]:
        async with semaphore:
            try:
                response = await client.get(url)
            except httpx.HTTPError:
                return url, None, url, "failed"
            status = response.status_code
            final_url = str(response.url)
            if status in CLOSED_STATUSES or (status == 200 and page_is_closed(response.text)):
                state = "closed"
            elif status in ACCEPTED_STATUSES:
                state = "protected" if status != 200 else "active"
            else:
                state = "failed"
            return url, status, final_url, state

    results = await asyncio.gather(*(check(str(row["canonical_apply_url"])) for row in rows))
    checked_at = datetime.now(UTC).isoformat()
    summary = LinkValidationSummary(checked=len(results))
    with db.connect() as connection:
        for url, http_status, final_url, state in results:
            connection.execute(
                """
                UPDATE postings
                SET link_checked_at=?, link_http_status=?, link_final_url=?, link_status=?
                WHERE canonical_apply_url=?
                """,
                (checked_at, http_status, final_url, state, url),
            )
            if state == "closed":
                connection.execute(
                    "UPDATE postings SET active=0 WHERE canonical_apply_url=?",
                    (url,),
                )
            setattr(summary, state, getattr(summary, state) + 1)
    if summary.closed:
        db.rebuild_families()
    return summary
