from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import httpx

from .collectors import CollectorResult
from .db import Database, _PsycopgConnectionAdapter
from .inventory import ClaimedTarget, WorkerSummary
from .inventory_runtime import (
    InventoryWorker as RuntimeInventoryWorker,
)
from .inventory_runtime import (
    RuntimeInventoryStore,
)


class LiveDatabase(Database):
    """Worker database connection that pipelines independent PostgreSQL writes."""

    @contextmanager
    def connect(self) -> Iterator[_PsycopgConnectionAdapter]:
        # apply_result historically issues one upsert per listing. Pipeline mode keeps
        # its transaction semantics while collapsing thousands of network round trips.
        with Database.connect(self) as adapter:
            with adapter._connection.pipeline():
                yield adapter


class LiveInventoryStore(RuntimeInventoryStore):
    """Production source lifecycle with optional provider-lane filtering."""

    def __init__(self, database: Database, worker_id: str) -> None:
        super().__init__(database, worker_id)
        raw_kinds = os.getenv("GAIA_WORKER_KINDS", "")
        self.allowed_kinds = sorted(
            {kind.strip() for kind in raw_kinds.split(",") if kind.strip()}
        )

    def claim_target(self, *, lease_seconds: int) -> ClaimedTarget | None:
        # Every GitHub Actions job can run many async workers, while several jobs run
        # simultaneously. PostgreSQL leases and SKIP LOCKED distribute sources safely.
        kinds = self.allowed_kinds or None
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
                      AND (%s::text[] IS NULL OR catalog.kind = ANY(%s::text[]))
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
                (kinds, kinds, self.worker_id, lease_seconds),
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
    """Horizontally scalable worker with safe retirement for vanished boards."""

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
        return await super()._run_discovery_if_due(client)
