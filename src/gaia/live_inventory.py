from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx

from .collectors import CollectorResult
from .db import Database
from .employer_census import refresh_employer_ecosystems
from .inventory import ClaimedTarget, WorkerSummary
from .inventory import InventoryWorker as BaseInventoryWorker
from .inventory_runtime import (
    InventoryWorker as RuntimeInventoryWorker,
)
from .inventory_runtime import (
    RuntimeInventoryStore,
)
from .source_catalog import _collector, save_catalog

LOGGER = logging.getLogger("gaia.inventory.live")


class LiveDatabase(Database):
    """Worker database using ordinary transaction-scoped PostgreSQL connections.

    A transaction-wide psycopg pipeline let one delayed statement poison every
    queued write in a source crawl. Psycopg's normal executemany path can still
    batch writes without making unrelated queries share one failure boundary.
    """


class LiveInventoryStore(RuntimeInventoryStore):
    """Production source lifecycle with optional provider-lane filtering."""

    def __init__(self, database: Database, worker_id: str) -> None:
        super().__init__(database, worker_id)
        raw_kinds = os.getenv("GAIA_WORKER_KINDS", "")
        self.allowed_kinds = sorted(
            {kind.strip() for kind in raw_kinds.split(",") if kind.strip()}
        )

    def claim_target(self, *, lease_seconds: int) -> ClaimedTarget | None:
        # Resolve the small validated catalog set before taking any row locks. A joined
        # SELECT ... FOR UPDATE caused PostgreSQL to choose a pathological plan under
        # several concurrent worker lanes, timing out before any source could be leased.
        # The second statement now operates only on crawl_targets' primary key and its
        # partial due index, while SKIP LOCKED still distributes work safely.
        kinds = self.allowed_kinds or None
        with self.database.connect() as connection:
            eligible_rows = connection.execute(
                """
                SELECT source
                FROM source_catalog
                WHERE validated
                  AND (%s::text[] IS NULL OR kind = ANY(%s::text[]))
                """,
                (kinds, kinds),
            ).fetchall()
            eligible_sources = [str(item["source"]) for item in eligible_rows]
            if not eligible_sources:
                return None

            row = connection.execute(
                """
                WITH candidate AS MATERIALIZED (
                    SELECT source
                    FROM crawl_targets
                    WHERE scheduled
                      AND enabled
                      AND source = ANY(%s::text[])
                      AND next_run_at <= now()
                      AND (lease_expires_at IS NULL OR lease_expires_at < now())
                    ORDER BY priority, next_run_at, source
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE crawl_targets AS target
                SET lease_owner=%s,
                    lease_expires_at=now() + (%s * interval '1 second'),
                    last_started_at=now(),
                    updated_at=now()
                FROM candidate
                WHERE target.source=candidate.source
                RETURNING target.source, target.interval_seconds,
                          target.consecutive_failures
                """,
                (eligible_sources, self.worker_id, lease_seconds),
            ).fetchone()

            if row is None:
                return None

            catalog = connection.execute(
                """
                SELECT kind, scope, spec
                FROM source_catalog
                WHERE source=%s
                """,
                (row["source"],),
            ).fetchone()

        if catalog is None:
            return None
        return ClaimedTarget(
            source=str(row["source"]),
            kind=str(catalog["kind"]),
            scope=str(catalog["scope"]),
            spec=dict(catalog["spec"] or {}),
            interval_seconds=int(row["interval_seconds"]),
            consecutive_failures=int(row["consecutive_failures"] or 0),
        )

    def finish_target(
        self,
        target: ClaimedTarget,
        result: CollectorResult,
        *,
        started_at: datetime,
        known_keys: set[str],
        active_keys: set[str],
    ) -> tuple[int, int]:
        dormant = result.status == "dormant"
        if dormant:
            # A missing board endpoint is not a complete empty snapshot. Preserve
            # previously active jobs until their application URLs are independently
            # confirmed closed or a replacement employer board is discovered.
            result.complete = False

        counts = super().finish_target(
            target,
            result,
            started_at=started_at,
            known_keys=known_keys,
            active_keys=active_keys,
        )

        if dormant:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE crawl_targets
                    SET enabled=FALSE,
                        scheduled=FALSE,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        last_status='dormant',
                        updated_at=now()
                    WHERE source=%s
                    """,
                    (target.source,),
                )
                connection.execute(
                    """
                    UPDATE source_catalog
                    SET validated=FALSE,
                        scope='historical',
                        origin='retired-dormant',
                        last_discovered_at=now()
                    WHERE source=%s
                    """,
                    (target.source,),
                )
        return counts


class InventoryWorker(RuntimeInventoryWorker):
    """Horizontally scalable worker with safe retirement and census expansion."""

    def __init__(self, database: Database, *, concurrency: int = 24) -> None:
        super().__init__(database, concurrency=concurrency)
        self.store = LiveInventoryStore(database, self.store.worker_id)

    async def run(
        self,
        *,
        once: bool = False,
        budget_seconds: float | None = None,
    ) -> WorkerSummary:
        # Provider lanes may mutate independent postings concurrently, but global
        # materialized read models must be rebuilt exactly once by `gaia reconcile`.
        skip_sync = os.getenv("GAIA_SKIP_CATALOG_SYNC", "0") == "1"
        defer_rebuild = os.getenv("GAIA_DEFER_FAMILY_REBUILD", "0") == "1"
        original_sync = self.store.sync_catalog
        original_rebuild = self.database.rebuild_families

        if skip_sync:
            self.store.sync_catalog = lambda: 0  # type: ignore[method-assign]
        if defer_rebuild:
            self.database.rebuild_families = lambda: None  # type: ignore[method-assign]
        try:
            return await super().run(once=once, budget_seconds=budget_seconds)
        finally:
            self.store.sync_catalog = original_sync  # type: ignore[method-assign]
            self.database.rebuild_families = original_rebuild  # type: ignore[method-assign]

    async def _run_discovery_if_due(self, client: httpx.AsyncClient) -> bool:
        if os.getenv("GAIA_ENABLE_DISCOVERY", "1") != "1":
            return False
        if self.store.allowed_kinds == ["__discovery_only__"]:
            # A dedicated discovery lane must not be starved by an unrelated board
            # backlog. It owns no source kinds, so it cannot duplicate inventory work.
            return await BaseInventoryWorker._run_discovery_if_due(self, client)
        return await super()._run_discovery_if_due(client)

    async def _refresh_market(
        self,
        client: httpx.AsyncClient,
        *,
        include_universe: bool,
    ) -> None:
        await super()._refresh_market(client, include_universe=include_universe)
        outcome = await refresh_employer_ecosystems(
            client,
            self.database,
            self.settings,
            refresh_feeds=include_universe,
            worker_id=self.store.worker_id,
            lease_seconds=self.lease_seconds,
        )
        LOGGER.info("employer ecosystem refresh %s", outcome)

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

        valid = (
            result.error is None
            and result.complete
            and result.status in {"ok", "empty"}
            and result.mode in {"board", "board-search", "domain"}
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
