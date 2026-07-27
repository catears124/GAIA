from __future__ import annotations

from .collectors import Collector
from .db import Database
from .inventory import ClaimedTarget, InventoryStore, InventoryWorker as BaseInventoryWorker

SUPPORTED_CATALOG_KINDS = {
    "greenhouse",
    "lever",
    "ashby",
    "workday-search",
    "smartrecruiters",
    "recruitee",
    "workable",
    "jobvite",
    "icims",
    "oracle-cloud",
    "successfactors",
    "domain",
    "verification",
    "google-careers",
}

FALLBACK_KINDS = {"domain", "verification"}
COVERAGE_KINDS = SUPPORTED_CATALOG_KINDS - FALLBACK_KINDS
BAD_COMPLETION_STATUSES = {"broken", "blocked", "truncated", "partial"}


class RuntimeInventoryStore(InventoryStore):
    """Schedule every useful source while measuring coverage on real board enumerators."""

    def sync_catalog(self) -> int:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT
                        catalog.source,
                        catalog.kind,
                        catalog.scope,
                        catalog.last_discovered_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY lower(catalog.source)
                            ORDER BY
                                (catalog.scope='current') DESC,
                                (
                                    health.complete
                                    AND health.last_success_at IS NOT NULL
                                    AND health.status <> ALL(%s)
                                ) DESC NULLS LAST,
                                health.last_success_at DESC NULLS LAST,
                                (catalog.source=lower(catalog.source)) DESC,
                                catalog.last_discovered_at DESC,
                                catalog.source
                        ) AS rank
                    FROM source_catalog AS catalog
                    LEFT JOIN source_health AS health USING(source)
                    WHERE catalog.kind = ANY(%s)
                )
                SELECT source, kind, scope
                FROM ranked
                WHERE rank=1
                ORDER BY source
                """,
                (sorted(BAD_COMPLETION_STATUSES), sorted(SUPPORTED_CATALOG_KINDS)),
            ).fetchall()

            # `enabled` now means "required for the public coverage contract".
            # `scheduled` controls whether the worker actually crawls the source.
            connection.execute(
                """
                UPDATE crawl_targets
                SET enabled=FALSE, scheduled=FALSE, updated_at=now()
                """
            )

            payload = [
                (
                    row["source"],
                    str(row["kind"]) in COVERAGE_KINDS,
                    self._priority(str(row["kind"]), str(row["scope"])),
                    self._default_interval(str(row["kind"]), str(row["scope"])),
                )
                for row in rows
            ]
            if payload:
                connection.executemany(
                    """
                    INSERT INTO crawl_targets(
                        source, enabled, scheduled, priority, interval_seconds
                    )
                    VALUES (%s,%s,TRUE,%s,%s)
                    ON CONFLICT(source) DO UPDATE SET
                        enabled=excluded.enabled,
                        scheduled=TRUE,
                        priority=excluded.priority,
                        interval_seconds=excluded.interval_seconds,
                        updated_at=now()
                    """,
                    payload,
                )

            # Preserve the hundreds of complete board checks imported from SQLite.
            # Without this bootstrap, the new scheduler falsely reports every known
            # source as never completed and needlessly recrawls the whole catalog at once.
            connection.execute(
                """
                UPDATE crawl_targets AS target
                SET
                    last_complete_at=COALESCE(target.last_complete_at, health.last_success_at),
                    last_finished_at=COALESCE(target.last_finished_at, health.last_attempt_at),
                    last_status=CASE
                        WHEN target.last_status='pending' THEN health.status
                        ELSE target.last_status
                    END,
                    last_rows=CASE
                        WHEN target.last_rows=0 THEN health.rows_scanned
                        ELSE target.last_rows
                    END,
                    expected_rows=COALESCE(target.expected_rows, health.expected_rows),
                    last_error=COALESCE(target.last_error, health.last_error),
                    next_run_at=CASE
                        WHEN target.last_complete_at IS NULL THEN GREATEST(
                            now(),
                            health.last_success_at
                                + make_interval(secs => target.interval_seconds)
                        )
                        ELSE target.next_run_at
                    END,
                    updated_at=now()
                FROM source_health AS health
                WHERE health.source=target.source
                  AND target.scheduled
                  AND health.complete
                  AND health.last_success_at IS NOT NULL
                  AND health.status <> ALL(%s)
                """,
                (sorted(BAD_COMPLETION_STATUSES),),
            )
        return len(payload)

    def claim_target(self, *, lease_seconds: int) -> ClaimedTarget | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT target.source
                    FROM crawl_targets AS target
                    JOIN source_catalog AS catalog USING(source)
                    WHERE target.scheduled
                      AND target.next_run_at<=now()
                      AND (target.lease_expires_at IS NULL OR target.lease_expires_at<now())
                    ORDER BY
                        CASE WHEN target.enabled THEN 0 ELSE 1 END,
                        target.priority,
                        target.next_run_at,
                        target.source
                    FOR UPDATE OF target SKIP LOCKED
                    LIMIT 1
                )
                UPDATE crawl_targets AS target
                SET lease_owner=%s,
                    lease_expires_at=now() + (%s * interval '1 second'),
                    last_started_at=now(),
                    updated_at=now()
                FROM candidate, source_catalog AS catalog
                WHERE target.source=candidate.source
                  AND catalog.source=target.source
                RETURNING target.source, catalog.kind, catalog.scope, catalog.spec,
                          target.interval_seconds, target.consecutive_failures
                """,
                (self.worker_id, lease_seconds),
            ).fetchone()
        if row is None:
            return None
        return ClaimedTarget(
            source=str(row["source"]),
            kind=str(row["kind"]),
            scope=str(row["scope"]),
            spec=dict(row["spec"] or {}),
            interval_seconds=int(row["interval_seconds"]),
            consecutive_failures=int(row["consecutive_failures"] or 0),
        )

    def ensure_task(self, key: str) -> None:
        # Existing employer boards are the first priority after deployment. Broad market
        # discovery starts shortly afterward; the heavier historical census starts later.
        initial_delay = 3600 if key == "universe-discovery" else 300
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_tasks(task_key, next_run_at)
                VALUES (%s, now() + (%s * interval '1 second'))
                ON CONFLICT(task_key) DO NOTHING
                """,
                (key, initial_delay),
            )


class InventoryWorker(BaseInventoryWorker):
    """Production worker with stable source identities and a filtered source queue."""

    def __init__(self, database: Database, *, concurrency: int = 12) -> None:
        super().__init__(database, concurrency=concurrency)
        self.store = RuntimeInventoryStore(database, self.store.worker_id)

    def _build_collector(self, target: ClaimedTarget) -> Collector | None:
        collector = super()._build_collector(target)
        if collector is not None:
            # The catalog key is the stable identity. Specs may preserve display casing.
            collector.name = target.source
        return collector
