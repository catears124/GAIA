from __future__ import annotations

from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected block not found in {path}: {old[:80]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


replace(
    "src/gaia/live_inventory.py",
    "from collections.abc import Iterator\nfrom contextlib import contextmanager\n",
    "",
)
replace(
    "src/gaia/live_inventory.py",
    "from .db import Database, _PsycopgConnectionAdapter",
    "from .db import Database",
)
replace(
    "src/gaia/live_inventory.py",
    '''class LiveDatabase(Database):
    """Worker database connection that pipelines independent PostgreSQL writes."""

    @contextmanager
    def connect(self) -> Iterator[_PsycopgConnectionAdapter]:
        # apply_result historically issues one upsert per listing. Pipeline mode keeps
        # its transaction semantics while collapsing thousands of network round trips.
        with Database.connect(self) as adapter:
            with adapter._connection.pipeline():
                yield adapter
''',
    '''class LiveDatabase(Database):
    """Worker database using ordinary transaction-scoped PostgreSQL connections.

    A transaction-wide psycopg pipeline let one delayed statement poison every
    queued write in a source crawl. Psycopg's normal executemany path can still
    batch writes without making unrelated queries share one failure boundary.
    """
''',
)
replace(
    "src/gaia/inventory.py",
    '''    async def _run_target(self, client: httpx.AsyncClient, target: ClaimedTarget) -> None:
        started_at = datetime.now(UTC)
        collector = self._build_collector(target)
        if collector is None:
            self.store.release_target(target, f"unsupported source kind: {target.kind}")
            self.summary.failed += 1
            return
        known, active = self.store.known_keys(target.source)
        try:
            result = self._normalize_result(collector, await collector.collect(client))
        except Exception as exc:
            result = self._failure_result(collector, exc)
        new, removed = self.store.finish_target(
            target,
            result,
            started_at=started_at,
            known_keys=known,
            active_keys=active,
        )
        self.summary.sources += 1
        self.summary.postings += len(result.postings)
        self.summary.new += new
        self.summary.removed += removed
        self.summary.failed += int(bool(result.error) or result.status in {"broken", "truncated"})
        LOGGER.info(
            "%s status=%s complete=%s rows=%s new=%s removed=%s",
            target.source,
            result.status,
            result.complete,
            result.rows_scanned,
            new,
            removed,
        )
''',
    '''    async def _run_target(self, client: httpx.AsyncClient, target: ClaimedTarget) -> None:
        started_at = datetime.now(UTC)
        collector = self._build_collector(target)
        if collector is None:
            self.store.release_target(target, f"unsupported source kind: {target.kind}")
            self.summary.failed += 1
            return
        try:
            known, active = self.store.known_keys(target.source)
        except Exception as exc:
            LOGGER.exception("source preparation failed for %s", target.source)
            try:
                self.store.release_target(target, repr(exc))
            except Exception:
                LOGGER.exception("could not release failed source %s", target.source)
            self.summary.failed += 1
            return
        try:
            result = self._normalize_result(collector, await collector.collect(client))
        except Exception as exc:
            result = self._failure_result(collector, exc)
        try:
            new, removed = self.store.finish_target(
                target,
                result,
                started_at=started_at,
                known_keys=known,
                active_keys=active,
            )
        except Exception as exc:
            # A slow or contended board write is a source failure, not a lane failure.
            # Keep the other concurrently claimed sources moving and release this lease.
            LOGGER.exception("source persistence failed for %s", target.source)
            try:
                self.store.release_target(target, repr(exc))
            except Exception:
                LOGGER.exception("could not release failed source %s", target.source)
            self.summary.failed += 1
            return
        self.summary.sources += 1
        self.summary.postings += len(result.postings)
        self.summary.new += new
        self.summary.removed += removed
        self.summary.failed += int(bool(result.error) or result.status in {"broken", "truncated"})
        LOGGER.info(
            "%s status=%s complete=%s rows=%s new=%s removed=%s",
            target.source,
            result.status,
            result.complete,
            result.rows_scanned,
            new,
            removed,
        )
''',
)

workflow = ".github/workflows/inventory.yml"
replace(workflow, "            workers: 24", "            workers: 8")
replace(
    workflow,
    '''          - lane: enterprise-a
            kinds: jobvite,icims,oracle-cloud,successfactors
            workers: 8
          - lane: enterprise-b
            kinds: jobvite,icims,oracle-cloud,successfactors
            workers: 8
''',
    '''          - lane: enterprise-a
            kinds: jobvite,icims,oracle-cloud,successfactors
            workers: 4
          - lane: enterprise-b
            kinds: jobvite,icims,oracle-cloud,successfactors
            workers: 4
''',
)
replace(
    workflow,
    '''          - lane: fallback
            kinds: domain,verification
            workers: 8
''',
    '''          - lane: fallback
            kinds: domain,verification
            workers: 4
''',
)
replace(
    workflow,
    '''      GAIA_AUTO_MIGRATE: "0"
      GAIA_READ_ONLY: "0"
''',
    '''      GAIA_AUTO_MIGRATE: "0"
      GAIA_DB_TIMEOUT: "240"
      GAIA_READ_ONLY: "0"
''',
)
replace(
    workflow,
    '''    needs: inventory
    if: ${{ always() && needs.inventory.result == 'success' }}
''',
    '''    needs: [prepare, inventory]
    if: ${{ always() && needs.prepare.result == 'success' }}
''',
)
replace(
    workflow,
    '''    needs: [inventory, discovery]
    if: ${{ always() && needs.inventory.result == 'success' && needs.discovery.result == 'success' }}
''',
    '''    needs: [prepare, inventory, discovery]
    if: ${{ always() && needs.prepare.result == 'success' }}
''',
)

Path("tests/test_inventory_stall_resilience.py").write_text(
    '''from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gaia.inventory import ClaimedTarget, InventoryWorker, WorkerSummary
from gaia.live_inventory import LiveDatabase
from gaia.db import Database
from gaia.models import CollectorResult


class _Collector:
    name = "greenhouse:test"
    scope = "current"

    async def collect(self, client):
        del client
        return CollectorResult(
            source=self.name,
            postings=[],
            complete=True,
            mode="board",
            rows_scanned=0,
            expected_rows=0,
            status="empty",
            scope=self.scope,
        )


class _FailingStore:
    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []

    def known_keys(self, source: str):
        del source
        return set(), set()

    def finish_target(self, *args, **kwargs):
        del args, kwargs
        raise TimeoutError("simulated statement timeout")

    def release_target(self, target: ClaimedTarget, error: str) -> None:
        self.released.append((target.source, error))


@pytest.mark.asyncio
async def test_source_write_timeout_does_not_kill_worker_lane(monkeypatch) -> None:
    worker = InventoryWorker.__new__(InventoryWorker)
    worker.summary = WorkerSummary()
    worker.store = _FailingStore()
    collector = _Collector()
    monkeypatch.setattr(worker, "_build_collector", lambda target: collector)
    monkeypatch.setattr(worker, "_normalize_result", lambda collector, result: result)
    target = ClaimedTarget(
        source="greenhouse:test",
        kind="greenhouse",
        scope="current",
        spec={},
        interval_seconds=900,
        consecutive_failures=0,
    )

    await worker._run_target(None, target)

    assert worker.summary.failed == 1
    assert worker.store.released
    assert worker.store.released[0][0] == "greenhouse:test"


def test_live_database_does_not_wrap_transactions_in_pipeline() -> None:
    assert LiveDatabase.connect is Database.connect


def test_partial_inventory_still_reconciles_read_models() -> None:
    workflow = open(".github/workflows/inventory.yml", encoding="utf-8").read()
    assert "workers: 24" not in workflow
    assert "GAIA_DB_TIMEOUT: \"240\"" in workflow
    assert "needs: [prepare, inventory, discovery]" in workflow
    assert "if: ${{ always() && needs.prepare.result == 'success' }}" in workflow
''',
    encoding="utf-8",
)
