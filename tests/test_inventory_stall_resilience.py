from __future__ import annotations

from pathlib import Path

import pytest

from gaia.db import Database
from gaia.inventory import ClaimedTarget, InventoryWorker, WorkerSummary
from gaia.live_inventory import LiveDatabase
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
    monkeypatch.setattr(worker, "_build_collector", lambda _target: collector)
    monkeypatch.setattr(worker, "_normalize_result", lambda _collector, result: result)
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


def test_inventory_is_provisioned_for_continuous_coverage() -> None:
    workflow = Path(".github/workflows/inventory.yml").read_text(encoding="utf-8")
    assert 'cron: "4,14,24,34,44,54 * * * *"' in workflow
    assert 'default: "900"' in workflow
    assert "group: gaia-production-inventory" in workflow
    assert "cancel-in-progress: false" in workflow
    assert '*) budget="480" ;;' in workflow
    assert "needs: repair" not in workflow
    assert "if: github.event_name != 'schedule'" in workflow
    assert "max-parallel: 6" in workflow
    assert "lane: greenhouse-lever" in workflow
    assert "lane: ashby" in workflow
    assert "lane: modern-ats" in workflow
    assert "lane: workday" in workflow
    assert "lane: enterprise" in workflow
    assert "lane: fallback-google" in workflow
    assert "kinds: workday-search" in workflow
    assert "workers: 4" in workflow
    assert "for attempt in 1 2 3" in workflow


def test_inventory_capacity_is_not_accidentally_reduced() -> None:
    workflow = Path(".github/workflows/inventory.yml").read_text(encoding="utf-8")
    worker_values = [
        int(line.split(":", 1)[1].strip())
        for line in workflow.splitlines()
        if line.strip().startswith("workers:")
    ]
    assert sum(worker_values) >= 18
    assert max(worker_values) >= 4


def test_source_maintenance_is_hourly_and_separate() -> None:
    workflow = Path(".github/workflows/maintenance.yml").read_text(encoding="utf-8")
    assert 'cron: "11 * * * *"' in workflow
    assert "group: gaia-production-maintenance" in workflow
    assert "run: gaia migrate" in workflow
    assert "GAIA_WORKER_KINDS: __discovery_only__" in workflow


def test_read_models_reconcile_independently_every_fifteen_minutes() -> None:
    workflow = Path(".github/workflows/reconcile.yml").read_text(encoding="utf-8")
    assert 'cron: "13,28,43,58 * * * *"' in workflow
    assert "group: gaia-production-reconcile" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "run: gaia reconcile" in workflow
    assert "gaia check" not in workflow


def test_health_recovery_is_contention_safe() -> None:
    workflow = Path(".github/workflows/production-health.yml").read_text(encoding="utf-8")
    assert 'cron: "2,17,32,47 * * * *"' in workflow
    assert 'GAIA_DB_TIMEOUT: "240"' in workflow
    assert "Ensure inventory pulse is active" in workflow
    assert "gh workflow run inventory.yml --ref main -f budget_seconds=900" in workflow
    assert "Fail only when recovery failed" in workflow
