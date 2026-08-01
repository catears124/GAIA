from __future__ import annotations

import asyncio
import logging
import os

import httpx

from .collectors import Collector
from .db import Database
from .discovery import (
    collectors_from_registry,
    load_universe_seed_postings,
    registry_collectors,
)
from .inventory import (
    ClaimedTarget,
    InventoryStore,
)
from .inventory import (
    InventoryWorker as BaseInventoryWorker,
)
from .models import CollectorResult, Posting
from .provider_discovery import provider_collectors_from_postings
from .quality import canonical_source_name
from .source_catalog import (
    _collector,
    _spec,
    merge_catalog,
    save_candidates,
    save_catalog,
)

LOGGER = logging.getLogger("gaia.inventory.runtime")

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
FALLBACK_KINDS = {"verification"}
COVERAGE_KINDS = SUPPORTED_CATALOG_KINDS - FALLBACK_KINDS
BAD_COMPLETION_STATUSES = {"broken", "blocked", "truncated", "partial", "dormant"}
VALID_CANDIDATE_STATUSES = {"ok", "empty"}
CANDIDATE_PROBE_TASK = "candidate-probe"
MARKET_DISCOVERY_TASK = "market-discovery"
UNIVERSE_DISCOVERY_TASK = "universe-discovery"


class RuntimeInventoryStore(InventoryStore):
    """Schedule only sources that have completed a real employer-board probe."""

    def sync_catalog(self) -> int:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT source, kind, scope
                FROM source_catalog
                WHERE validated
                  AND kind = ANY(%s)
                ORDER BY source
                """,
                (sorted(SUPPORTED_CATALOG_KINDS),),
            ).fetchall()

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

    def ensure_task(self, key: str) -> None:
        initial_delay = {
            UNIVERSE_DISCOVERY_TASK: 3600,
            CANDIDATE_PROBE_TASK: 60,
        }.get(key, 300)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_tasks(task_key, next_run_at)
                VALUES (%s, now() + (%s * interval '1 second'))
                ON CONFLICT(task_key) DO NOTHING
                """,
                (key, initial_delay),
            )

    def task_is_overdue(self, key: str, *, delay_seconds: int) -> bool:
        """Return true when an unleased task has exceeded its scheduling grace."""
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM worker_tasks
                    WHERE task_key=%s
                      AND next_run_at <= now() - (%s * interval '1 second')
                      AND (lease_expires_at IS NULL OR lease_expires_at<now())
                ) AS overdue
                """,
                (key, max(0, delay_seconds)),
            ).fetchone()
        return bool(row["overdue"])

    def has_due_coverage_targets(self) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM crawl_targets AS target
                    JOIN source_catalog AS catalog USING(source)
                    WHERE target.enabled
                      AND target.scheduled
                      AND catalog.validated
                      AND target.next_run_at<=now()
                      AND (
                          target.lease_expires_at IS NULL
                          OR target.lease_expires_at<now()
                      )
                ) AS due
                """
            ).fetchone()
        return bool(row["due"])

    def claim_target(self, *, lease_seconds: int) -> ClaimedTarget | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT target.source
                    FROM crawl_targets AS target
                    JOIN source_catalog AS catalog USING(source)
                    WHERE target.scheduled
                      AND catalog.validated
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

    def claim_candidates(self, *, limit: int, lease_seconds: int) -> list[ClaimedTarget]:
        if limit <= 0:
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH selected AS (
                    SELECT source
                    FROM source_candidates
                    WHERE status IN ('candidate','retry')
                      AND next_probe_at<=now()
                      AND (lease_expires_at IS NULL OR lease_expires_at<now())
                    ORDER BY
                        (scope='current') DESC,
                        evidence_count DESC,
                        next_probe_at,
                        source
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE source_candidates AS candidate
                SET lease_owner=%s,
                    lease_expires_at=now() + (%s * interval '1 second'),
                    last_probe_at=now()
                FROM selected
                WHERE candidate.source=selected.source
                RETURNING candidate.source, candidate.kind, candidate.scope, candidate.spec,
                          candidate.consecutive_failures
                """,
                (limit, self.worker_id, lease_seconds),
            ).fetchall()
        return [
            ClaimedTarget(
                source=str(row["source"]),
                kind=str(row["kind"]),
                scope=str(row["scope"]),
                spec=dict(row["spec"] or {}),
                interval_seconds=self._default_interval(str(row["kind"]), str(row["scope"])),
                consecutive_failures=int(row["consecutive_failures"] or 0),
            )
            for row in rows
        ]

    def finish_candidate(
        self,
        target: ClaimedTarget,
        *,
        promoted: bool,
        status: str,
        error: str | None,
    ) -> None:
        with self.database.connect() as connection:
            if promoted:
                connection.execute(
                    "DELETE FROM source_candidates WHERE source=%s AND lease_owner=%s",
                    (target.source, self.worker_id),
                )
                return
            failures = target.consecutive_failures + 1
            rejected = status == "dormant" or failures >= 5
            delay = min(7 * 86400, 6 * 3600 * (2 ** min(failures - 1, 4)))
            connection.execute(
                """
                UPDATE source_candidates
                SET status=%s,
                    next_probe_at=now() + (%s * interval '1 second'),
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    consecutive_failures=%s,
                    last_error=%s
                WHERE source=%s AND lease_owner=%s
                """,
                (
                    "rejected" if rejected else "retry",
                    delay,
                    failures,
                    error or status,
                    target.source,
                    self.worker_id,
                ),
            )


class _RecursiveDiscoveryCollector(Collector):
    def __init__(self, worker: InventoryWorker, delegate: Collector) -> None:
        self.worker = worker
        self.delegate = delegate
        self.name = delegate.name
        self.mode = delegate.mode
        self.scope = delegate.scope

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        result = await self.delegate.collect(client)
        self.worker._save_recursive_candidates(
            result.discovery_postings,
            origin=f"recursive-source:{self.name}",
        )
        return result


class InventoryWorker(BaseInventoryWorker):
    """Production worker with reserved capacity for source expansion."""

    def __init__(self, database: Database, *, concurrency: int = 12) -> None:
        super().__init__(database, concurrency=concurrency)
        self.store = RuntimeInventoryStore(database, self.store.worker_id)
        self.candidate_probe_interval = max(
            60, int(os.getenv("GAIA_CANDIDATE_PROBE_INTERVAL", "300"))
        )
        self.discovery_starvation_seconds = max(
            5 * 60, int(os.getenv("GAIA_DISCOVERY_STARVATION_SECONDS", "1800"))
        )

    def _candidate_collectors(self, postings: list[Posting]) -> list[Collector]:
        generated = merge_catalog(
            collectors_from_registry(postings, self.settings, deep=True),
            provider_collectors_from_postings(postings),
        )
        candidates: list[Collector] = []
        for collector in generated:
            described = _spec(collector)
            if described is None or described[0] not in COVERAGE_KINDS:
                continue
            collector.name = canonical_source_name(collector.name)
            candidates.append(collector)
        return candidates

    def _save_recursive_candidates(self, postings: list[Posting], *, origin: str) -> int:
        if not postings:
            return 0
        candidates = self._candidate_collectors(postings)
        if not candidates:
            return 0
        saved = save_candidates(self.database, candidates, origin=origin)
        LOGGER.info(
            "recursive discovery origin=%s evidence=%s candidates=%s",
            origin,
            len(postings),
            saved,
        )
        return saved

    def _build_collector(self, target: ClaimedTarget) -> Collector | None:
        collector = super()._build_collector(target)
        if collector is None:
            return None
        collector.name = target.source
        return _RecursiveDiscoveryCollector(self, collector)

    async def _probe_candidate(
        self,
        client: httpx.AsyncClient,
        target: ClaimedTarget,
    ) -> bool:
        collector = _collector(target.kind, target.spec)
        if collector is None:
            self.store.finish_candidate(
                target,
                promoted=False,
                status="broken",
                error=f"unsupported source kind: {target.kind}",
            )
            return False
        collector.scope = target.scope
        collector.name = target.source
        try:
            result = self._normalize_result(collector, await collector.collect(client))
        except Exception as exc:
            result = self._failure_result(collector, exc)

        self._save_recursive_candidates(
            result.discovery_postings,
            origin=f"candidate-probe:{target.source}",
        )
        valid = (
            result.error is None
            and result.complete
            and result.status in VALID_CANDIDATE_STATUSES
            and result.mode in {"board", "board-search"}
        )
        if valid:
            try:
                self.database.apply_result(result, rebuild=False)
                save_catalog(
                    self.database,
                    [collector],
                    validated=True,
                    origin="validated-candidate-probe",
                )
            except Exception as exc:
                LOGGER.exception("candidate persistence failed for %s", target.source)
                self.store.finish_candidate(
                    target,
                    promoted=False,
                    status="broken",
                    error=repr(exc),
                )
                return False
            self.store.finish_candidate(
                target,
                promoted=True,
                status=result.status,
                error=None,
            )
            LOGGER.info(
                "promoted source candidate %s rows=%s",
                target.source,
                result.rows_scanned,
            )
            return True

        if result.error:
            self.database.record_failure(result)
        self.store.finish_candidate(
            target,
            promoted=False,
            status=result.status,
            error=result.error or result.note,
        )
        return False

    async def _probe_due_candidates(
        self,
        client: httpx.AsyncClient,
        *,
        limit: int,
    ) -> tuple[int, int]:
        claimed = self.store.claim_candidates(
            limit=limit,
            lease_seconds=self.lease_seconds,
        )
        promoted = 0
        if claimed:
            promoted = sum(
                await asyncio.gather(
                    *(self._probe_candidate(client, target) for target in claimed)
                )
            )
        if promoted:
            self.store.sync_catalog()
            self.database.rebuild_families()
        return len(claimed), promoted

    async def _run_candidate_probe_if_due(self, client: httpx.AsyncClient) -> bool:
        """Drain source candidates independently of expensive market refreshes."""
        self.store.ensure_task(CANDIDATE_PROBE_TASK)
        if not self.store.claim_task(CANDIDATE_PROBE_TASK, lease_seconds=self.lease_seconds):
            return False

        limit = max(1, int(os.getenv("GAIA_CANDIDATE_PROBE_LIMIT", "24")))
        try:
            claimed, promoted = await self._probe_due_candidates(client, limit=limit)
        except Exception as exc:
            LOGGER.exception("candidate probe task failed")
            self.store.finish_task(
                CANDIDATE_PROBE_TASK,
                interval_seconds=60,
                status="broken",
                error=repr(exc),
            )
        else:
            # Keep draining at one-minute cadence while a full batch exists; back
            # off to the normal cadence once the due queue is smaller than a batch.
            interval = 60 if claimed >= limit else self.candidate_probe_interval
            self.store.finish_task(
                CANDIDATE_PROBE_TASK,
                interval_seconds=interval,
                status="ok",
            )
            self.summary.discovery_runs += 1
            LOGGER.info(
                "candidate probe claimed=%s promoted=%s next_seconds=%s",
                claimed,
                promoted,
                interval,
            )
        return True

    async def _run_discovery_if_due(self, client: httpx.AsyncClient) -> bool:
        # Candidate validation is cheap relative to market enumeration and must not
        # wait for every fast-cadence board to become current.
        if await self._run_candidate_probe_if_due(client):
            return True

        self.store.ensure_task(MARKET_DISCOVERY_TASK)
        self.store.ensure_task(UNIVERSE_DISCOVERY_TASK)
        coverage_due = self.store.has_due_coverage_targets()
        market_starved = self.store.task_is_overdue(
            MARKET_DISCOVERY_TASK,
            delay_seconds=self.discovery_starvation_seconds,
        )
        if coverage_due and not market_starved:
            return False
        if coverage_due and market_starved:
            LOGGER.warning(
                "market discovery exceeded %ss grace; reserving one expansion cycle",
                self.discovery_starvation_seconds,
            )
        return await super()._run_discovery_if_due(client)

    async def _refresh_market(self, client: httpx.AsyncClient, *, include_universe: bool) -> None:
        """Use curated lists as evidence only; never let search results mutate production."""
        discovery_postings: list[Posting] = []
        collectors = registry_collectors(self.settings)
        registry_results = await asyncio.gather(
            *(collector.collect(client) for collector in collectors),
            return_exceptions=True,
        )
        for collector, outcome in zip(collectors, registry_results, strict=True):
            if isinstance(outcome, Exception):
                result = self._failure_result(collector, outcome)
            else:
                result = outcome
            if result.error:
                self.database.record_failure(result)
            else:
                self.database.apply_result(result, rebuild=False)
                discovery_postings.extend(result.postings)
                discovery_postings.extend(result.discovery_postings)

        if include_universe:
            universe_postings, universe_health = await load_universe_seed_postings(
                client, self.settings
            )
            await self._apply_auxiliary_results(universe_health)
            discovery_postings.extend(universe_postings)

        candidates = self._candidate_collectors(discovery_postings)
        save_candidates(
            self.database,
            candidates,
            origin="historical-universe" if include_universe else "curated-registry",
        )

        limit = max(1, int(os.getenv("GAIA_CANDIDATE_PROBE_LIMIT", "24")))
        claimed, promoted = await self._probe_due_candidates(client, limit=limit)

        self.store.sync_catalog()
        self.database.rebuild_families()
        self.summary.discovery_runs += 1
        LOGGER.info(
            "discovery evidence=%s probed=%s promoted=%s dynamic_github_search=disabled",
            len(candidates),
            claimed,
            promoted,
        )
