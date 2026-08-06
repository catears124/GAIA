from __future__ import annotations

import asyncio
from collections import defaultdict
from urllib.parse import urlsplit

from .collectors import SchemaPageCollector
from .db import Database
from .lead_promotion import (
    _BACKSTOP_MODES,
    _claim_due_leads,
    _filter_recovered,
    _result_lead_urls,
)
from .models import CollectorResult, Posting, canonical_url


async def verify_fresh_leads(
    database: Database,
    *,
    limit: int,
    concurrency: int,
    max_age_days: int = 2,
) -> dict[str, object]:
    """Verify a small fresh-lead batch without reports or a global feed rebuild.

    The database-clocked caller publishes the family projection and drains Discord after
    this function commits. Keeping those responsibilities separate makes the verifier
    fit comfortably inside Vercel's hard request deadline.
    """
    from .dynamic_market_discovery import _client

    bounded_limit = max(1, min(int(limit), 8))
    workers = max(1, min(int(concurrency), 6))
    bounded_age = max(1, min(int(max_age_days), 7))
    leads = _claim_due_leads(
        database,
        limit=bounded_limit,
        max_age_days=bounded_age,
    )
    grouped: dict[tuple[str, str], list[Posting]] = defaultdict(list)
    for lead in leads:
        grouped[(lead.company, urlsplit(lead.canonical_apply_url).netloc.casefold())].append(
            lead
        )

    semaphore = asyncio.Semaphore(workers)

    async def collect_group(
        client,
        company: str,
        host: str,
        items: list[Posting],
    ) -> tuple[list[Posting], CollectorResult]:
        async with semaphore:
            collector = SchemaPageCollector(
                company,
                name=f"lead-verification:{host}:{company}",
                leads=items,
                trusted=True,
            )
            try:
                result = await collector.collect(client)
            except Exception as error:  # noqa: BLE001 - isolate one employer page.
                result = CollectorResult(
                    source=collector.name,
                    postings=[],
                    complete=False,
                    mode="verification",
                    rows_scanned=0,
                    error=repr(error),
                    status="broken",
                    scope="current",
                )
            return items, _filter_recovered(result, items)

    results: list[tuple[list[Posting], CollectorResult]] = []
    async with _client(workers) as client:
        if grouped:
            results = await asyncio.gather(
                *(
                    collect_group(client, company, host, items)
                    for (company, host), items in grouped.items()
                )
            )

    recovered_urls: set[str] = set()
    verified_lead_urls: set[str] = set()
    closed_urls: set[str] = set()
    attempted_status: dict[str, str] = {}
    blocked_groups = 0
    unstructured_groups = 0
    failed_groups = 0

    for items, result in results:
        item_urls = {item.canonical_apply_url for item in items}
        recovered_urls.update(item.canonical_apply_url for item in result.postings)
        matched_leads = _result_lead_urls(result, items)
        verified_lead_urls.update(matched_leads)
        group_closed = {canonical_url(url) for url in result.closed_urls}
        closed_urls.update(group_closed)

        unresolved = item_urls - matched_leads - group_closed
        status = (
            "blocked"
            if result.status == "blocked"
            else "error"
            if result.error
            else "unverified"
        )
        for url in unresolved:
            attempted_status[url] = status

        if result.postings or result.closed_urls:
            database.apply_result(result, rebuild=False)
        elif result.error:
            database.record_failure(result)
        if result.status == "blocked":
            blocked_groups += 1
        elif result.status == "unstructured":
            unstructured_groups += 1
        elif result.error:
            failed_groups += 1

    attempted_urls = set(attempted_status)
    if recovered_urls or verified_lead_urls or closed_urls or attempted_urls:
        with database.connect() as connection:
            if verified_lead_urls:
                connection.execute(
                    """
                    UPDATE postings
                    SET link_checked_at=now(), link_status='verified',
                        link_final_url=canonical_apply_url
                    WHERE active
                      AND source_mode = ANY(%s)
                      AND canonical_apply_url = ANY(%s)
                    """,
                    (list(_BACKSTOP_MODES), sorted(verified_lead_urls)),
                )
            if closed_urls:
                connection.execute(
                    """
                    UPDATE postings
                    SET active=FALSE, removed_at=COALESCE(removed_at,now()),
                        link_checked_at=now(), link_status='closed'
                    WHERE active
                      AND source_mode = ANY(%s)
                      AND canonical_apply_url = ANY(%s)
                    """,
                    (list(_BACKSTOP_MODES), sorted(closed_urls)),
                )
            for status in ("blocked", "error", "unverified"):
                urls = sorted(
                    url
                    for url, observed_status in attempted_status.items()
                    if observed_status == status
                )
                if urls:
                    connection.execute(
                        """
                        UPDATE postings
                        SET link_checked_at=now(), link_status=%s
                        WHERE active
                          AND source_mode = ANY(%s)
                          AND canonical_apply_url = ANY(%s)
                        """,
                        (status, list(_BACKSTOP_MODES), urls),
                    )

    return {
        "status": "ok",
        "strategy": "bounded_fresh_lead_employer_page_verification",
        "selected_leads": len(leads),
        "companies": len({item.company for item in leads}),
        "groups": len(grouped),
        "recovered_verified_openings": len(recovered_urls),
        "verified_leads": len(verified_lead_urls),
        "closed_leads": len(closed_urls),
        "deferred_unresolved_leads": len(attempted_urls),
        "blocked_groups": blocked_groups,
        "unstructured_groups": unstructured_groups,
        "failed_groups": failed_groups,
    }
