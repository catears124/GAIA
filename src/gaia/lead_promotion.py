from __future__ import annotations

import asyncio
from collections import defaultdict
from urllib.parse import urlsplit

from .collectors import SchemaPageCollector
from .conversion_funnel import build_report
from .db_base import TARGET_MATCHES, application_identity
from .models import CollectorResult, Posting, canonical_url
from .page_verification import title_similarity
from .quality import is_actionable_application_url

_BACKSTOP_MODES = ("registry", "external-index", "verification-lead")


def _identity(row: dict[str, object]) -> str:
    return application_identity(
        str(row["canonical_apply_url"]),
        str(row["source"]),
        str(row["source_id"]),
    )


def _load_due_leads(database, *, limit: int, max_age_days: int) -> list[Posting]:
    scan_limit = max(limit * 8, 128)
    with database.connect() as connection:
        verified_rows = connection.execute(
            """
            SELECT canonical_apply_url,source,source_id
            FROM postings
            WHERE active
              AND source_mode IN ('direct','verification')
              AND target_match = ANY(%s)
            """,
            (list(TARGET_MATCHES),),
        ).fetchall()
        lead_rows = connection.execute(
            """
            SELECT posting_key,company,title,locations,apply_url,canonical_apply_url,
              source,source_id,source_mode,description,employment_type,posted_at,
              updated_at,posted_raw,posted_precision,posted_confidence,first_seen_at,
              last_seen_at,category,season,year,target_match,link_status,link_checked_at
            FROM postings
            WHERE active
              AND source_mode = ANY(%s)
              AND target_match = ANY(%s)
              AND first_seen_at >= now() - (%s * interval '1 day')
              AND COALESCE(link_status,'unchecked') NOT IN ('closed','invalid','verified')
              AND (
                link_checked_at IS NULL
                OR link_checked_at < now() - interval '6 hours'
              )
            ORDER BY
              (first_seen_at >= now() - interval '48 hours') DESC,
              (COALESCE(link_status,'unchecked')='unchecked') DESC,
              first_seen_at DESC,
              last_seen_at DESC,
              posting_key
            LIMIT %s
            """,
            (list(_BACKSTOP_MODES), list(TARGET_MATCHES), max_age_days, scan_limit),
        ).fetchall()

    verified_identities = {_identity(dict(row)) for row in verified_rows}
    selected: list[Posting] = []
    seen: set[str] = set()
    for raw in lead_rows:
        row = dict(raw)
        identity = _identity(row)
        if identity in verified_identities or identity in seen:
            continue
        url = str(row["canonical_apply_url"])
        if not is_actionable_application_url(url):
            continue
        host = urlsplit(url).netloc.casefold()
        if not host:
            continue
        seen.add(identity)
        selected.append(
            Posting(
                company=str(row["company"]),
                title=str(row["title"]),
                apply_url=str(row["apply_url"]),
                source=str(row["source"]),
                source_id=str(row["source_id"]),
                locations=list(row.get("locations") or []),
                source_mode=str(row["source_mode"]),
                description=str(row.get("description") or ""),
                employment_type=str(row.get("employment_type") or ""),
                posted_at=row.get("posted_at"),
                updated_at=row.get("updated_at"),
                posted_raw=row.get("posted_raw"),
                posted_precision=str(row.get("posted_precision") or "unknown"),
                posted_confidence=str(row.get("posted_confidence") or "unknown"),
                category=str(row.get("category") or "other"),
                season=row.get("season"),
                year=row.get("year"),
                target_match=str(row.get("target_match") or "unknown"),
            )
        )
        if len(selected) >= limit:
            break
    return selected


def _best_lead(posting: Posting, leads: list[Posting]) -> tuple[Posting | None, float]:
    exact = next(
        (item for item in leads if item.canonical_apply_url == posting.canonical_apply_url),
        None,
    )
    if exact is not None:
        return exact, title_similarity(exact.title, posting.title)
    ranked = sorted(
        ((title_similarity(item.title, posting.title), item) for item in leads),
        key=lambda item: item[0],
        reverse=True,
    )
    return (ranked[0][1], ranked[0][0]) if ranked else (None, 0.0)


def _filter_recovered(result: CollectorResult, leads: list[Posting]) -> CollectorResult:
    accepted: list[Posting] = []
    for posting in result.postings:
        if posting.target_match not in TARGET_MATCHES:
            continue
        if not is_actionable_application_url(posting.canonical_apply_url):
            continue
        lead, similarity = _best_lead(posting, leads)
        if lead is None:
            continue
        exact_url = lead.canonical_apply_url == posting.canonical_apply_url
        if similarity < (0.55 if exact_url else 0.8):
            continue
        posting.source_mode = "verification"
        accepted.append(posting)

    result.postings = accepted
    if not accepted and result.status == "verified":
        result.status = "unstructured"
    return result


def _result_lead_urls(result: CollectorResult, leads: list[Posting]) -> set[str]:
    matched: set[str] = set()
    for posting in result.postings:
        lead, similarity = _best_lead(posting, leads)
        if lead is None:
            continue
        exact_url = lead.canonical_apply_url == posting.canonical_apply_url
        if similarity >= (0.55 if exact_url else 0.8):
            matched.add(lead.canonical_apply_url)
    return matched


async def promote_leads(
    *,
    limit: int,
    concurrency: int,
    hours: int,
    max_age_days: int = 14,
) -> dict[str, object]:
    from .dynamic_market_discovery import _client
    from .live_inventory import LiveDatabase

    bounded_limit = max(1, min(int(limit), 64))
    workers = max(1, min(int(concurrency), 12))
    bounded_age = max(1, min(int(max_age_days), 90))
    database = LiveDatabase(migrate=False)
    before = build_report(database, hours=hours, limit=min(bounded_limit, 50))
    before_funnel = dict(before.get("funnel") or {})
    before_jobs = int(before_funnel.get("new_verified_jobs_window") or 0)

    leads = _load_due_leads(
        database,
        limit=bounded_limit,
        max_age_days=bounded_age,
    )
    grouped: dict[tuple[str, str], list[Posting]] = defaultdict(list)
    for lead in leads:
        grouped[(lead.company, urlsplit(lead.canonical_apply_url).netloc.casefold())].append(lead)

    semaphore = asyncio.Semaphore(workers)

    async def collect_group(client, company: str, host: str, items: list[Posting]):
        async with semaphore:
            collector = SchemaPageCollector(
                company,
                name=f"lead-verification:{host}:{company}",
                leads=items,
                trusted=True,
            )
            return items, _filter_recovered(await collector.collect(client), items)

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
                    url for url, observed_status in attempted_status.items()
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

    database.rebuild_families()
    after = build_report(database, hours=hours, limit=min(bounded_limit, 50))
    after_funnel = dict(after.get("funnel") or {})
    after_jobs = int(after_funnel.get("new_verified_jobs_window") or 0)
    return {
        "status": "ok",
        "strategy": "fresh_actionable_leads_to_employer_page_verification",
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
        "verified_jobs_delta": after_jobs - before_jobs,
        "before": before,
        "after": after,
    }
