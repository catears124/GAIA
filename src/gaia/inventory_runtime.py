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


class RuntimeInventoryStore(InventoryStore):
    """Queue only real employer enumerators, not registry/diagnostic catalog rows."""

    def sync_catalog(self) -> int:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT source, kind, scope
                FROM source_catalog
                WHERE kind = ANY(%s)
                ORDER BY source
                """,
                (sorted(SUPPORTED_CATALOG_KINDS),),
            ).fetchall()
            payload = [
                (
                    row["source"],
                    self._priority(str(row["kind"]), str(row["scope"])),
                    self._default_interval(str(row["kind"]), str(row["scope"])),
                )
                for row in rows
            ]
            if payload:
                connection.executemany(
                    """
                    INSERT INTO crawl_targets(source, priority, interval_seconds)
                    VALUES (%s,%s,%s)
                    ON CONFLICT(source) DO UPDATE SET
                        priority=excluded.priority,
                        interval_seconds=excluded.interval_seconds,
                        enabled=TRUE,
                        updated_at=now()
                    """,
                    payload,
                )
            connection.execute(
                """
                UPDATE crawl_targets AS target
                SET enabled=FALSE, updated_at=now()
                FROM source_catalog AS catalog
                WHERE catalog.source=target.source
                  AND NOT (catalog.kind = ANY(%s))
                """,
                (sorted(SUPPORTED_CATALOG_KINDS),),
            )
        return len(payload)


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
