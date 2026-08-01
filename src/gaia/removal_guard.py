from __future__ import annotations

import os
import re
from datetime import UTC, datetime

from .db_base import TARGET_MATCHES, application_identity
from .db_write import WriteMixin
from .grouping import family_key, normalize_title
from .models import CollectorResult, canonical_url
from .quality import normalize_locations

_DEFAULT_REMOVAL_GRACE_SECONDS = 30 * 60
_MAX_INTERVAL_GRACE_SECONDS = 6 * 60 * 60
_DEFAULT_LEGACY_RESTORE_SECONDS = 6 * 60 * 60


class GuardedWriteMixin(WriteMixin):
    """Persist complete board snapshots without trusting a single omission.

    A provider can briefly return a partial-but-successful payload. The legacy writer
    interpreted one such response as authoritative and immediately erased every
    omitted listing. Here, a missing direct posting remains active in a pending-close
    state until another complete crawl arrives after a source-aware grace period.
    Explicit closed URLs and stale non-direct evidence can still close immediately.
    """

    def _removal_grace_seconds(self, db, source: str) -> int:
        configured = max(
            60,
            int(
                os.getenv(
                    "GAIA_REMOVAL_GRACE_SECONDS",
                    str(_DEFAULT_REMOVAL_GRACE_SECONDS),
                )
            ),
        )
        row = db.execute(
            "SELECT interval_seconds FROM crawl_targets WHERE source=%s",
            (source,),
        ).fetchone()
        interval = int(row["interval_seconds"] or 0) if row else 0
        return max(configured, min(interval, _MAX_INTERVAL_GRACE_SECONDS))

    def apply_result(
        self,
        result: CollectorResult,
        *,
        rebuild: bool = True,
        run_id: int | None = None,
    ) -> None:
        observed = datetime.now(UTC)
        postings = [
            posting
            for posting in result.postings
            if posting.company.strip()
            and posting.title.strip()
            and posting.canonical_apply_url.strip()
        ]
        for posting in postings:
            posting.locations = normalize_locations(posting.locations)
        current_keys = {posting.posting_key for posting in postings}

        with self.connect() as db:
            # One-time compatibility repair for direct rows that the old one-scan
            # deletion path hard-disabled without recording removed_at. They receive
            # a fresh pending-close grace window and will either be recovered by the
            # provider or confirmed closed on a later complete crawl.
            legacy_restore_seconds = max(
                60,
                int(
                    os.getenv(
                        "GAIA_LEGACY_REMOVAL_RESTORE_SECONDS",
                        str(_DEFAULT_LEGACY_RESTORE_SECONDS),
                    )
                ),
            )
            db.execute(
                """
                UPDATE postings
                SET active=TRUE, removed_at=%s
                WHERE NOT active
                  AND removed_at IS NULL
                  AND source_mode='direct'
                  AND last_seen_at >= %s - make_interval(secs => %s)
                """,
                (observed, observed, legacy_restore_seconds),
            )

            old_keys = {
                str(row["posting_key"])
                for row in db.execute(
                    "SELECT posting_key FROM postings WHERE source=%s AND active",
                    (result.source,),
                )
            }
            previous_target_identities: set[str] = set()
            if result.complete and result.mode in {"board", "board-search"}:
                previous_target_identities = {
                    application_identity(
                        str(row["canonical_apply_url"]),
                        str(row["source"]),
                        str(row["source_id"]),
                    )
                    for row in db.execute(
                        """
                        SELECT canonical_apply_url, source, source_id
                        FROM postings
                        WHERE source=%s AND target_match = ANY(%s)
                        """,
                        (result.source, list(TARGET_MATCHES)),
                    )
                }

            for posting in postings:
                stored_description = (
                    posting.description if posting.target_match in TARGET_MATCHES else ""
                )
                db.execute(
                    """
                    INSERT INTO postings(
                        posting_key, family_key, company, title, normalized_title, locations,
                        apply_url, canonical_apply_url, source, source_id, source_mode, description,
                        employment_type, posted_at, updated_at, posted_raw, posted_precision,
                        posted_confidence, first_seen_at, last_seen_at, active, category, season,
                        year, target_match, removed_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        TRUE,%s,%s,%s,%s,NULL
                    )
                    ON CONFLICT(posting_key) DO UPDATE SET
                        family_key=excluded.family_key,
                        company=excluded.company,
                        title=excluded.title,
                        normalized_title=excluded.normalized_title,
                        locations=excluded.locations,
                        apply_url=excluded.apply_url,
                        canonical_apply_url=excluded.canonical_apply_url,
                        source_mode=excluded.source_mode,
                        description=excluded.description,
                        employment_type=excluded.employment_type,
                        posted_at=COALESCE(excluded.posted_at, postings.posted_at),
                        updated_at=COALESCE(excluded.updated_at, postings.updated_at),
                        posted_raw=COALESCE(excluded.posted_raw, postings.posted_raw),
                        posted_precision=CASE
                            WHEN excluded.posted_at IS NOT NULL THEN excluded.posted_precision
                            ELSE postings.posted_precision END,
                        posted_confidence=CASE
                            WHEN excluded.posted_at IS NOT NULL THEN excluded.posted_confidence
                            ELSE postings.posted_confidence END,
                        last_seen_at=excluded.last_seen_at,
                        active=TRUE,
                        removed_at=NULL,
                        category=excluded.category,
                        season=excluded.season,
                        year=excluded.year,
                        target_match=excluded.target_match
                    """,
                    (
                        posting.posting_key,
                        family_key(posting),
                        posting.company,
                        posting.title,
                        normalize_title(posting.title),
                        sorted(set(posting.locations)),
                        posting.apply_url,
                        posting.canonical_apply_url,
                        posting.source,
                        posting.source_id,
                        posting.source_mode,
                        stored_description,
                        posting.employment_type,
                        posting.posted_at,
                        posting.updated_at,
                        posting.posted_raw,
                        posting.posted_precision,
                        posting.posted_confidence,
                        observed,
                        observed,
                        posting.category,
                        posting.season,
                        posting.year,
                        posting.target_match,
                    ),
                )

            if result.complete:
                missing = sorted(old_keys - current_keys)
                if missing:
                    grace_seconds = self._removal_grace_seconds(db, result.source)
                    db.execute(
                        """
                        UPDATE postings
                        SET removed_at=COALESCE(removed_at, %s)
                        WHERE posting_key = ANY(%s) AND active
                        """,
                        (observed, missing),
                    )
                    db.execute(
                        """
                        UPDATE postings
                        SET active=FALSE
                        WHERE posting_key = ANY(%s)
                          AND active
                          AND removed_at <= %s - make_interval(secs => %s)
                        """,
                        (missing, observed, grace_seconds),
                    )

                current_target_identities = {
                    application_identity(
                        posting.canonical_apply_url,
                        posting.source,
                        posting.source_id,
                    )
                    for posting in postings
                    if posting.target_match in TARGET_MATCHES
                }
                if result.source.startswith("greenhouse:") and result.status in {"ok", "empty"}:
                    board = result.source.partition(":")[2].lower()
                    provider_stale_keys = [
                        str(row["posting_key"])
                        for row in db.execute(
                            """
                            SELECT posting_key, company, canonical_apply_url, source, source_id
                            FROM postings
                            WHERE active
                              AND source_mode IN ('registry','external-index','verification-lead')
                            """
                        )
                        if re.sub(r"[^a-z0-9]", "", str(row["company"]).lower()) == board
                        and application_identity(
                            str(row["canonical_apply_url"]),
                            str(row["source"]),
                            str(row["source_id"]),
                        )
                        not in current_target_identities
                    ]
                    if provider_stale_keys:
                        db.execute(
                            """
                            UPDATE postings
                            SET active=FALSE, removed_at=COALESCE(removed_at, %s)
                            WHERE posting_key = ANY(%s)
                            """,
                            (observed, provider_stale_keys),
                        )

                stale_identities = previous_target_identities - current_target_identities
                if stale_identities:
                    stale_keys = [
                        str(row["posting_key"])
                        for row in db.execute(
                            """
                            SELECT posting_key, canonical_apply_url, source, source_id
                            FROM postings
                            WHERE active
                              AND source_mode IN (
                                  'registry', 'external-index', 'verification-lead'
                              )
                              AND target_match = ANY(%s)
                            """,
                            (list(TARGET_MATCHES),),
                        )
                        if application_identity(
                            str(row["canonical_apply_url"]),
                            str(row["source"]),
                            str(row["source_id"]),
                        )
                        in stale_identities
                    ]
                    if stale_keys:
                        db.execute(
                            """
                            UPDATE postings
                            SET active=FALSE, removed_at=COALESCE(removed_at, %s)
                            WHERE posting_key = ANY(%s)
                            """,
                            (observed, stale_keys),
                        )

            if result.closed_urls:
                closed = sorted({canonical_url(url) for url in result.closed_urls})
                db.execute(
                    """
                    UPDATE postings
                    SET active=FALSE, removed_at=COALESCE(removed_at, %s)
                    WHERE canonical_apply_url = ANY(%s)
                      AND source_mode IN ('registry','verification','external-index')
                    """,
                    (observed, closed),
                )

            target_rows = sum(posting.target_match in TARGET_MATCHES for posting in result.postings)
            last_success = (
                observed
                if result.error is None and result.status not in {"blocked", "broken"}
                else None
            )
            productive = any(
                posting.source_mode in {"direct", "verification"}
                and posting.target_match in TARGET_MATCHES
                for posting in postings
            )
            db.execute(
                """
                INSERT INTO source_health(
                    source, mode, complete, rows_scanned, expected_rows, target_rows,
                    last_attempt_at, last_success_at, last_error, status, scope, note, last_run_id,
                    lifecycle, consecutive_failures
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)
                ON CONFLICT(source) DO UPDATE SET
                    mode=excluded.mode,
                    complete=excluded.complete,
                    rows_scanned=excluded.rows_scanned,
                    expected_rows=excluded.expected_rows,
                    target_rows=excluded.target_rows,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=COALESCE(excluded.last_success_at, source_health.last_success_at),
                    last_error=excluded.last_error,
                    status=excluded.status,
                    scope=CASE WHEN excluded.lifecycle='productive' THEN 'current' ELSE excluded.scope END,
                    note=excluded.note,
                    last_run_id=excluded.last_run_id,
                    lifecycle=CASE
                        WHEN excluded.lifecycle='productive' THEN 'productive'
                        WHEN source_health.lifecycle='quarantined' THEN 'candidate'
                        ELSE source_health.lifecycle
                    END,
                    consecutive_failures=0
                """,
                (
                    result.source,
                    result.mode,
                    result.complete,
                    result.rows_scanned,
                    result.expected_rows,
                    target_rows,
                    observed,
                    last_success,
                    result.error,
                    result.status,
                    result.scope,
                    result.note,
                    run_id,
                    "productive" if productive else "candidate",
                ),
            )

        if rebuild:
            self.rebuild_families()
