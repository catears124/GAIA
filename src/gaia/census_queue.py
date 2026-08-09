from __future__ import annotations

from typing import Any

from .collectors import Collector
from .db import Database
from .quality import canonical_source_name
from .source_catalog import save_candidates


def enqueue_net_new_candidates(
    database: Database,
    collectors: list[Collector],
    *,
    origin: str,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Insert census discoveries exactly once without reviving failed probes.

    Census inputs are intentionally repeatable: platform sitemaps run frequently and
    archived Common Crawl snapshots can be replayed after a database outage. Calling
    save_candidates directly on every replay would reset retry/rejected rows back to
    candidate and inflate evidence_count forever. A census is existence evidence, not
    a reason to erase the validator's lifecycle decision, so only truly unseen source
    names are inserted here.
    """
    names = {
        canonical_source_name(collector.name)
        for collector in collectors
        if canonical_source_name(collector.name)
    }
    with database.connect() as connection:
        validated = {
            str(row["source"])
            for row in connection.execute(
                "SELECT source FROM source_catalog WHERE validated"
            ).fetchall()
        }
        queued = {
            str(row["source"])
            for row in connection.execute("SELECT source FROM source_candidates").fetchall()
        }
        connection.execute(
            """
            DELETE FROM source_candidates AS candidate
            USING source_catalog AS catalog
            WHERE candidate.source=catalog.source
              AND catalog.validated
            """
        )

    known = validated | queued
    unseen = [
        collector
        for collector in collectors
        if canonical_source_name(collector.name) not in known
    ]
    chunk = max(25, min(int(batch_size), 1000))
    written = 0
    for start in range(0, len(unseen), chunk):
        written += save_candidates(
            database,
            unseen[start : start + chunk],
            origin=origin,
        )

    return {
        "candidate_rows_in_snapshot": len(collectors),
        "candidate_unique_names": len(names),
        "candidate_rows_already_validated": sum(
            canonical_source_name(collector.name) in validated for collector in collectors
        ),
        "candidate_rows_already_queued": sum(
            canonical_source_name(collector.name) in queued for collector in collectors
        ),
        "candidate_rows_written": written,
        "candidate_validation_deferred": True,
    }
