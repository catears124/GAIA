from __future__ import annotations

from datetime import datetime

from .collectors import CollectorResult
from .db import Database
from .inventory import ClaimedTarget
from .inventory_runtime import (
    InventoryWorker as RuntimeInventoryWorker,
    RuntimeInventoryStore,
)


class LiveInventoryStore(RuntimeInventoryStore):
    """Production source lifecycle on top of the validated inventory queue."""

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
    """Continuous worker with safe retirement for vanished employer boards."""

    def __init__(self, database: Database, *, concurrency: int = 24) -> None:
        super().__init__(database, concurrency=concurrency)
        self.store = LiveInventoryStore(database, self.store.worker_id)
