from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

TARGETS = "('exact','year_confirmed','source_confirmed')"


def _rows(connection: Any, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _row(connection: Any, sql: str, params: tuple[object, ...] = ()) -> dict[str, Any]:
    return dict(connection.execute(sql, params).fetchone() or {})


def _safe_rows(connection: Any, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    try:
        return _rows(connection, sql, params)
    except Exception as error:
        return [{"diagnostic_error": repr(error)}]


def _safe_row(connection: Any, sql: str, params: tuple[object, ...] = ()) -> dict[str, Any]:
    try:
        return _row(connection, sql, params)
    except Exception as error:
        return {"diagnostic_error": repr(error)}


def reason_bucket(reason: object) -> str:
    value = str(reason or "unknown").casefold()
    groups = (
        ("timeout", ("timeout", "timed out")),
        ("dns", ("enotfound", "name or service not known", "dns")),
        ("tls", ("ssl", "tls", "certificate")),
        ("blocked_or_rate_limited", ("403", "429", "blocked", "forbidden", "rate limit")),
        ("missing_or_dormant", ("404", "410", "dormant", "not found", "gone")),
        ("incomplete_enumeration", ("truncated", "partial", "incomplete", "pagination")),
        ("parse_or_schema", ("parse", "decode", "json", "xml", "schema")),
        ("persistence", ("database", "postgres", "supabase", "persist", "constraint")),
        ("unsupported_source", ("unsupported", "unknown source kind")),
        (
            "classification_rejected",
            ("not_internship", "wrong_year", "wrong_season", "no relevant"),
        ),
    )
    for label, tokens in groups:
        if any(token in value for token in tokens):
            return label
    if value in {"", "none", "unknown", "candidate", "retry", "rejected"}:
        return "unclassified"
    return "other"


def failure_counts(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if "diagnostic_error" in row:
            continue
        reason = next(
            (
                row.get(key)
                for key in (
                    "last_error",
                    "error",
                    "note",
                    "last_status",
                    "status",
                    "target_match",
                )
                if row.get(key)
            ),
            "unknown",
        )
        bucket = reason_bucket(reason)
        counts[bucket] += 1
        rendered = str(reason)[:240]
        if rendered not in examples[bucket] and len(examples[bucket]) < 3:
            examples[bucket].append(rendered)
    return [
        {"reason": label, "count": count, "examples": examples[label]}
        for label, count in counts.most_common(25)
    ]


def _ratio(numerator: object, denominator: object) -> float | None:
    top, bottom = int(numerator or 0), int(denominator or 0)
    return round(top / bottom, 4) if bottom else None


def _errors(sections: dict[str, object]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for section, value in sections.items():
        for row in value if isinstance(value, list) else [value]:
            if isinstance(row, dict) and row.get("diagnostic_error"):
                found.append({"section": section, "error": str(row["diagnostic_error"])})
    return found


def build_report(database: Any, *, hours: int = 24, limit: int = 50) -> dict[str, object]:
    window = max(1, min(int(hours), 720))
    sample = max(1, min(int(limit), 200))
    with database.connect() as connection:
        families = _safe_row(
            connection,
            f"""
            SELECT
              COUNT(*) FILTER (
                WHERE target_match IN {TARGETS} AND direct_openings>0
              ) active_verified_jobs,
              COALESCE(SUM(direct_openings) FILTER (
                WHERE target_match IN {TARGETS} AND direct_openings>0
              ),0) active_verified_openings,
              COUNT(DISTINCT company) FILTER (
                WHERE target_match IN {TARGETS} AND direct_openings>0
              ) active_verified_companies,
              COUNT(*) FILTER (
                WHERE target_match IN {TARGETS}
                  AND direct_openings>0
                  AND first_detected_at>=now()-(%s*interval '1 hour')
              ) new_verified_jobs_window,
              COUNT(*) FILTER (
                WHERE target_match!='not_internship'
                  AND direct_openings>0
                  AND first_detected_at>=now()-(%s*interval '1 hour')
              ) new_visible_jobs_window,
              MAX(first_detected_at) FILTER (
                WHERE target_match IN {TARGETS} AND direct_openings>0
              ) newest_verified_detected_at,
              MAX(latest_posted_at) FILTER (
                WHERE target_match IN {TARGETS} AND direct_openings>0
              ) newest_verified_employer_posted_at
            FROM families
            """,
            (window, window),
        )
        postings = _safe_row(
            connection,
            f"""
            SELECT
              COUNT(*) FILTER (
                WHERE first_seen_at>=now()-(%s*interval '1 hour')
              ) discovered_postings_window,
              COUNT(*) FILTER (
                WHERE first_seen_at>=now()-(%s*interval '1 hour')
                  AND target_match IN {TARGETS}
              ) classified_target_postings_window,
              COUNT(*) FILTER (
                WHERE first_seen_at>=now()-(%s*interval '1 hour')
                  AND target_match IN {TARGETS}
                  AND source_mode IN ('direct','verification')
              ) verified_postings_window,
              COUNT(*) FILTER (
                WHERE first_seen_at>=now()-(%s*interval '1 hour')
                  AND target_match IN ('not_internship','wrong_year','wrong_season')
              ) rejected_postings_window
            FROM postings
            """,
            (window, window, window, window),
        )
        sources = _safe_row(
            connection,
            """
            SELECT
              (SELECT COUNT(*) FROM source_catalog WHERE validated) validated_sources,
              (SELECT COUNT(*) FROM source_catalog
                WHERE validated
                  AND origin='validated-candidate-probe'
                  AND first_discovered_at>=now()-(%s*interval '1 hour'))
                promoted_sources_window,
              (SELECT COUNT(*) FROM source_candidates) candidate_sources_total,
              (SELECT COUNT(*) FROM source_candidates WHERE status='candidate')
                candidate_sources_unprobed,
              (SELECT COUNT(*) FROM source_candidates WHERE status='retry')
                candidate_sources_retry,
              (SELECT COUNT(*) FROM source_candidates WHERE status='rejected')
                candidate_sources_rejected,
              (SELECT COUNT(*) FROM source_candidates
                WHERE status IN ('candidate','retry')
                  AND next_probe_at<=now()
                  AND (lease_expires_at IS NULL OR lease_expires_at<now()))
                candidate_sources_due,
              (SELECT MIN(next_probe_at) FROM source_candidates
                WHERE status IN ('candidate','retry')
                  AND next_probe_at<=now()
                  AND (lease_expires_at IS NULL OR lease_expires_at<now()))
                oldest_due_candidate_at,
              (SELECT COUNT(*) FROM source_catalog c
                LEFT JOIN crawl_targets t USING(source)
                WHERE c.validated AND (t.source IS NULL OR NOT t.scheduled))
                validated_sources_unscheduled
            """,
            (window,),
        )
        snapshots = _safe_row(
            connection,
            """
            SELECT COUNT(*) crawl_attempts_window,
              COUNT(DISTINCT source) crawled_sources_window,
              COUNT(*) FILTER (WHERE complete AND error IS NULL) complete_crawls_window,
              COUNT(*) FILTER (WHERE NOT complete OR error IS NOT NULL) failed_crawls_window,
              COALESCE(SUM(rows_scanned),0) rows_scanned_window,
              COALESCE(SUM(target_rows),0) target_rows_window,
              COALESCE(SUM(new_rows),0) new_rows_window,
              COUNT(*) FILTER (
                WHERE complete AND error IS NULL AND rows_scanned>0 AND target_rows=0
              ) complete_zero_target_crawls_window,
              COUNT(*) FILTER (
                WHERE complete AND error IS NULL AND new_rows=0
              ) complete_zero_new_crawls_window
            FROM source_snapshots
            WHERE finished_at>=now()-(%s*interval '1 hour')
            """,
            (window,),
        )
        publication = _safe_row(
            connection,
            f"""
            SELECT COUNT(*) verified_postings_missing_family,
              COUNT(DISTINCT p.company) affected_companies
            FROM postings p
            WHERE p.active
              AND p.target_match IN {TARGETS}
              AND p.source_mode IN ('direct','verification')
              AND NOT EXISTS (
                SELECT 1 FROM families f WHERE f.family_key=p.family_key
              )
            """,
        )
        publication_gaps = _safe_rows(
            connection,
            f"""
            SELECT p.posting_key,p.family_key,p.company,p.title,p.source,p.source_mode,
              p.target_match,p.first_seen_at,p.posted_at,p.canonical_apply_url
            FROM postings p
            WHERE p.active
              AND p.target_match IN {TARGETS}
              AND p.source_mode IN ('direct','verification')
              AND NOT EXISTS (
                SELECT 1 FROM families f WHERE f.family_key=p.family_key
              )
            ORDER BY p.first_seen_at DESC
            LIMIT %s
            """,
            (sample,),
        )
        candidates = _safe_rows(
            connection,
            """
            SELECT source,kind,scope,origin,evidence_count,first_seen_at,last_seen_at,
              next_probe_at,last_probe_at,status,consecutive_failures,last_error,
              lease_owner,lease_expires_at
            FROM source_candidates
            ORDER BY (status IN ('retry','rejected')) DESC,
              consecutive_failures DESC,
              COALESCE(last_probe_at,last_seen_at,first_seen_at) DESC
            LIMIT %s
            """,
            (sample,),
        )
        rejected = _safe_rows(
            connection,
            f"""
            SELECT posting_key,company,title,source,source_mode,target_match,category,
              first_seen_at,posted_at,canonical_apply_url
            FROM postings
            WHERE first_seen_at>=now()-(%s*interval '1 hour')
              AND target_match NOT IN {TARGETS}
            ORDER BY first_seen_at DESC
            LIMIT %s
            """,
            (window, sample),
        )
        zero_yield = _safe_rows(
            connection,
            """
            SELECT source,COUNT(*) attempts,COALESCE(SUM(rows_scanned),0) rows_scanned,
              COALESCE(SUM(target_rows),0) target_rows,
              COALESCE(SUM(new_rows),0) new_rows,
              MAX(finished_at) last_finished_at,
              MAX(error) FILTER (WHERE error IS NOT NULL) error
            FROM source_snapshots
            WHERE finished_at>=now()-(%s*interval '1 hour')
            GROUP BY source
            HAVING COALESCE(SUM(new_rows),0)=0
            ORDER BY COALESCE(SUM(rows_scanned),0) DESC,
              MAX(finished_at) DESC
            LIMIT %s
            """,
            (window, sample),
        )
        stalled = _safe_rows(
            connection,
            """
            SELECT t.source,c.kind,t.next_run_at,t.last_complete_at,t.last_status,
              t.last_rows,t.consecutive_failures,t.last_error,t.lease_expires_at
            FROM crawl_targets t
            JOIN source_catalog c USING(source)
            WHERE c.validated AND t.scheduled AND (
              t.next_run_at<=now()-interval '30 minutes'
              OR t.last_complete_at IS NULL
              OR t.last_complete_at<now()-(
                GREATEST(t.interval_seconds*3,3600)*interval '1 second'
              )
              OR t.consecutive_failures>0
            )
            ORDER BY t.consecutive_failures DESC,t.next_run_at,
              t.last_complete_at NULLS FIRST
            LIMIT %s
            """,
            (sample,),
        )
        recent_snapshots = _safe_rows(
            connection,
            """
            SELECT source,started_at,finished_at,status,complete,rows_scanned,
              expected_rows,target_rows,new_rows,removed_rows,error
            FROM source_snapshots
            ORDER BY finished_at DESC
            LIMIT %s
            """,
            (sample,),
        )
        tasks = _safe_rows(
            connection,
            """
            SELECT task_key,next_run_at,lease_owner,lease_expires_at,last_started_at,
              last_finished_at,last_status,last_error,
              (next_run_at<=now()
                AND (lease_expires_at IS NULL OR lease_expires_at<now())) overdue
            FROM worker_tasks
            ORDER BY overdue DESC,next_run_at,task_key
            """,
        )

    sections = {
        "families": families,
        "postings": postings,
        "sources": sources,
        "snapshots": snapshots,
        "publication": publication,
        "publication_gaps": publication_gaps,
        "candidates": candidates,
        "rejected": rejected,
        "zero_yield": zero_yield,
        "stalled": stalled,
        "recent_snapshots": recent_snapshots,
        "tasks": tasks,
    }
    discovered = int(postings.get("discovered_postings_window") or 0)
    verified_postings = int(postings.get("verified_postings_window") or 0)
    target_rows = int(snapshots.get("target_rows_window") or 0)
    scanned = int(snapshots.get("rows_scanned_window") or 0)
    verified_jobs = int(families.get("new_verified_jobs_window") or 0)
    promoted = int(sources.get("promoted_sources_window") or 0)
    return {
        "objective": "increase_verified_new_jobs",
        "generated_at": datetime.now(UTC).isoformat(),
        "window_hours": window,
        "funnel": {
            **families,
            **postings,
            **snapshots,
            **publication,
            "discovered_posting_to_verified_posting_rate": _ratio(
                verified_postings, discovered
            ),
            "rows_scanned_to_target_row_rate": _ratio(target_rows, scanned),
            "promoted_source_to_verified_job_rate": _ratio(verified_jobs, promoted),
        },
        "sources": sources,
        "blockers": {
            "candidate_failures": failure_counts(candidates),
            "crawl_failures": failure_counts(
                [
                    row
                    for row in recent_snapshots
                    if row.get("error") or not row.get("complete", True)
                ]
            ),
            "classification_rejections": failure_counts(rejected),
        },
        "samples": {
            "source_candidates": candidates,
            "publication_gap_postings": publication_gaps,
            "recent_rejected_postings": rejected,
            "zero_yield_sources": zero_yield,
            "stalled_sources": stalled,
            "recent_source_snapshots": recent_snapshots,
            "worker_tasks": tasks,
        },
        "diagnostic_errors": _errors(sections),
        "sample_limit": sample,
    }


def repair_publication(database: Any, *, hours: int = 24, limit: int = 50) -> dict[str, object]:
    before = build_report(database, hours=hours, limit=limit)
    before_funnel = dict(before.get("funnel") or {})
    missing_before = int(before_funnel.get("verified_postings_missing_family") or 0)
    jobs_before = int(before_funnel.get("new_verified_jobs_window") or 0)

    rebuilt = database.rebuild_families()

    after = build_report(database, hours=hours, limit=limit)
    after_funnel = dict(after.get("funnel") or {})
    missing_after = int(after_funnel.get("verified_postings_missing_family") or 0)
    jobs_after = int(after_funnel.get("new_verified_jobs_window") or 0)
    return {
        "status": "ok",
        "families_rebuilt": int(rebuilt or 0),
        "publication_gaps_before": missing_before,
        "publication_gaps_after": missing_after,
        "publication_gaps_repaired": max(0, missing_before - missing_after),
        "verified_jobs_delta": jobs_after - jobs_before,
        "before": before,
        "after": after,
    }


async def drain_candidates(*, limit: int, concurrency: int, hours: int) -> dict[str, object]:
    from .dynamic_market_discovery import _client
    from .live_inventory import InventoryWorker, LiveDatabase

    probe_limit = max(1, min(int(limit), 64))
    workers = max(1, min(int(concurrency), 12))
    database = LiveDatabase(migrate=False)
    before = build_report(database, hours=hours, limit=min(probe_limit, 50))
    before_funnel = dict(before.get("funnel") or {})
    before_jobs = int(before_funnel.get("new_verified_jobs_window") or 0)
    gaps_before = int(before_funnel.get("verified_postings_missing_family") or 0)

    # A Vercel invocation has a much shorter useful lifetime than a full crawler.
    # Keep abandoned candidate leases short so a timeout cannot starve thousands
    # of due candidates for the 30-minute general crawl lease.
    candidate_lease = max(
        60,
        min(
            int(os.getenv("GAIA_DIAGNOSTIC_CANDIDATE_LEASE_SECONDS", "120")),
            300,
        ),
    )
    worker = InventoryWorker(database, concurrency=workers)
    worker.lease_seconds = candidate_lease

    # Always repair the read model before and after probing. This publishes jobs
    # already verified in postings but missing from the families table even when
    # the candidate batch contains no promotable source.
    database.rebuild_families()
    async with _client(workers) as client:
        claimed, promoted = await worker._probe_due_candidates(
            client,
            limit=probe_limit,
        )
    database.rebuild_families()

    after = build_report(database, hours=hours, limit=min(probe_limit, 50))
    after_funnel = dict(after.get("funnel") or {})
    after_jobs = int(after_funnel.get("new_verified_jobs_window") or 0)
    gaps_after = int(after_funnel.get("verified_postings_missing_family") or 0)
    return {
        "status": "ok",
        "candidate_lease_seconds": candidate_lease,
        "claimed_candidates": claimed,
        "promoted_sources": promoted,
        "publication_gaps_before": gaps_before,
        "publication_gaps_after": gaps_after,
        "publication_gaps_repaired": max(0, gaps_before - gaps_after),
        "verified_jobs_delta": after_jobs - before_jobs,
        "before": before,
        "after": after,
    }
