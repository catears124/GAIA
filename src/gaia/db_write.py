from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from .db_base import (
    EMPLOYER_DATE_MODES,
    TARGET_MATCHES,
    _source_sort,
    _target_sort,
    application_identity,
    iso,
)
from .grouping import family_key, normalize_title
from .models import CollectorResult, canonical_url
from .quality import is_actionable_application_url, normalize_locations


class WriteMixin:
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
                        year, target_match
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        TRUE,%s,%s,%s,%s
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
                    db.execute(
                        "UPDATE postings SET active=FALSE WHERE posting_key = ANY(%s)",
                        (missing,),
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
                            "UPDATE postings SET active=FALSE WHERE posting_key = ANY(%s)",
                            (provider_stale_keys,),
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
                            "UPDATE postings SET active=FALSE WHERE posting_key = ANY(%s)",
                            (stale_keys,),
                        )

            if result.closed_urls:
                closed = sorted({canonical_url(url) for url in result.closed_urls})
                db.execute(
                    """
                    UPDATE postings SET active=FALSE
                    WHERE canonical_apply_url = ANY(%s)
                      AND source_mode IN ('registry','verification','external-index')
                    """,
                    (closed,),
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

    def record_failure(self, result: CollectorResult, *, run_id: int | None = None) -> None:
        now = datetime.now(UTC)
        quarantine_after = max(1, int(os.getenv("GAIA_SOURCE_QUARANTINE_FAILURES", "3")))
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO source_health(
                    source, mode, complete, rows_scanned, expected_rows, target_rows,
                    last_attempt_at, last_success_at, last_error, status, scope, note, last_run_id,
                    lifecycle, consecutive_failures
                ) VALUES (%s,%s,FALSE,%s,%s,0,%s,NULL,%s,%s,%s,%s,%s,'candidate',1)
                ON CONFLICT(source) DO UPDATE SET
                    mode=excluded.mode,
                    complete=FALSE,
                    rows_scanned=excluded.rows_scanned,
                    expected_rows=excluded.expected_rows,
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error,
                    status=excluded.status,
                    scope=CASE
                        WHEN source_health.consecutive_failures + 1 >= %s THEN 'historical'
                        ELSE excluded.scope
                    END,
                    note=excluded.note,
                    last_run_id=excluded.last_run_id,
                    lifecycle=CASE
                        WHEN source_health.consecutive_failures + 1 >= %s THEN 'quarantined'
                        ELSE source_health.lifecycle
                    END,
                    consecutive_failures=source_health.consecutive_failures + 1
                """,
                (
                    result.source,
                    result.mode,
                    result.rows_scanned,
                    result.expected_rows,
                    now,
                    result.error,
                    result.status,
                    result.scope,
                    result.note,
                    run_id,
                    quarantine_after,
                    quarantine_after,
                ),
            )

    def rebuild_families(self) -> None:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT
                    posting_key,
                    family_key,
                    company,
                    title,
                    locations,
                    apply_url,
                    canonical_apply_url,
                    source,
                    source_id,
                    source_mode,
                    posted_at,
                    posted_precision,
                    first_seen_at,
                    last_seen_at,
                    category,
                    season,
                    year,
                    target_match
                FROM postings
                WHERE active AND target_match!='not_internship'
                """
            ).fetchall()
            blocked_keys = [
                str(row["posting_key"])
                for row in rows
                if not is_actionable_application_url(str(row["canonical_apply_url"]))
            ]
            if blocked_keys:
                db.execute(
                    "UPDATE postings SET active=FALSE WHERE posting_key = ANY(%s)",
                    (blocked_keys,),
                )
                blocked = set(blocked_keys)
                rows = [row for row in rows if str(row["posting_key"]) not in blocked]
            db.execute("DELETE FROM families")

            variants_by_application: dict[str, list[Mapping[str, Any]]] = {}
            for row in rows:
                identity = application_identity(
                    str(row["canonical_apply_url"]),
                    str(row["source"]),
                    str(row["source_id"]),
                )
                variants_by_application.setdefault(identity, []).append(row)

            applications_by_family: dict[str, list[dict[str, Any]]] = {}
            for identity, variants in variants_by_application.items():
                if all(str(row["source_mode"]) == "verification-lead" for row in variants):
                    continue
                selected = min(variants, key=_source_sort)
                target_anchor = max(variants, key=_target_sort)
                canonical_family = str(target_anchor["family_key"])
                locations = sorted(
                    {
                        location
                        for row in variants
                        for location in (row.get("locations") or [])
                        if location
                    }
                )
                employer_date_rows = [
                    row
                    for row in variants
                    if row["posted_at"] and str(row["source_mode"]) in EMPLOYER_DATE_MODES
                ]
                employer_dates = sorted(row["posted_at"] for row in employer_date_rows)
                independently_recovered = any(
                    str(row["source_mode"]) == "direct" for row in variants
                )
                first_detected = min(
                    row["first_seen_at"] for row in variants if row["first_seen_at"]
                )
                last_verified = max(
                    row["last_seen_at"] for row in variants if row["last_seen_at"]
                )
                application = {
                    "identity": identity,
                    "selected": selected,
                    "target_anchor": target_anchor,
                    "variants": variants,
                    "locations": locations,
                    "employer_dates": employer_dates,
                    "employer_precisions": [
                        str(row["posted_precision"]) for row in employer_date_rows
                    ],
                    "independently_recovered": independently_recovered,
                    "opening": {
                        "application_identity": identity,
                        "posting_key": selected["posting_key"],
                        "location": locations,
                        "apply_url": selected["apply_url"],
                        "source": selected["source"],
                        "source_mode": selected["source_mode"],
                        "posted_at": iso(employer_dates[0]) if employer_dates else None,
                        "first_detected_at": iso(first_detected),
                        "last_verified_at": iso(last_verified),
                        "source_variants": sorted(
                            {f"{row['source_mode']}:{row['source']}" for row in variants}
                        ),
                    },
                }
                applications_by_family.setdefault(canonical_family, []).append(application)

            def family_rows() -> Iterable[tuple[Any, ...]]:
                for key, applications in applications_by_family.items():
                    selected_rows = [app["selected"] for app in applications]
                    preferred = min(selected_rows, key=_source_sort)
                    anchors = [app["target_anchor"] for app in applications]
                    target_anchor = max(anchors, key=_target_sort)
                    target = str(target_anchor["target_match"])
                    locations = sorted(
                        {
                            location
                            for application in applications
                            for location in application["locations"]
                        }
                    )
                    openings = [application["opening"] for application in applications]
                    openings.sort(key=lambda item: (item["location"], item["apply_url"]))
                    employer_dates = sorted(
                        date
                        for application in applications
                        for date in application["employer_dates"]
                    )
                    precisions = [
                        precision
                        for application in applications
                        for precision in application["employer_precisions"]
                    ]
                    precision = (
                        "timestamp"
                        if "timestamp" in precisions
                        else ("day" if "day" in precisions else "unknown")
                    )
                    variant_rows = [
                        row for application in applications for row in application["variants"]
                    ]
                    first_seen = min(row["first_seen_at"] for row in variant_rows)
                    last_seen = max(row["last_seen_at"] for row in variant_rows)
                    independent_openings = sum(
                        bool(app["independently_recovered"]) for app in applications
                    )
                    yield (
                        key,
                        preferred["company"],
                        preferred["title"],
                        preferred["category"],
                        target_anchor["season"],
                        target_anchor["year"],
                        target,
                        len(openings),
                        len(locations),
                        locations,
                        Jsonb(openings),
                        employer_dates[0] if employer_dates else None,
                        employer_dates[-1] if employer_dates else None,
                        precision,
                        first_seen,
                        last_seen,
                        independent_openings,
                        len(applications) - independent_openings,
                    )

            db.executemany(
                """
                INSERT INTO families(
                    family_key, company, title, category, season, year, target_match,
                    opening_count, location_count, locations, openings, first_posted_at,
                    latest_posted_at, posted_precision, first_detected_at, last_verified_at,
                    direct_openings, backstop_openings
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                family_rows(),
            )
