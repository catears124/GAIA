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
    monkeypatch.setattr(
        worker,
        "_normalize_result",
        lambda _collector, result: result,
    )
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


def test_inventory_lanes_pulse_independently_every_fifteen_minutes() -> None:
    workflow = Path(".github/workflows/inventory.yml").read_text(encoding="utf-8")
    assert 'cron: "4,19,34,49 * * * *"' in workflow
    assert 'default: "900"' in workflow
    assert "group: gaia-production-inventory-${{ matrix.lane }}" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'workflow_dispatch' }}" in workflow
    assert '*) budget="480" ;;' in workflow
    assert "needs: prepare" not in workflow
    assert "Migrate and maintain source queue" not in workflow
    assert "Employer census and source validation" not in workflow
    assert "state=pending" in workflow
    assert "Provider pulse superseded by recovery" in workflow


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
    assert "run: gaia reconcile" in workflow
    assert "gaia check" not in workflow


def test_health_recovery_is_contention_safe() -> None:
    workflow = Path(".github/workflows/production-health.yml").read_text(encoding="utf-8")
    assert 'cron: "2,17,32,47 * * * *"' in workflow
    assert 'GAIA_DB_TIMEOUT: "240"' in workflow
    assert '".github/workflows/inventory.yml"' not in workflow
    assert '".github/workflows/maintenance.yml"' not in workflow
    assert '".github/workflows/reconcile.yml"' not in workflow
    assert '.event == "workflow_dispatch"' in workflow
    assert "gh workflow run inventory.yml --ref main -f budget_seconds=900" in workflow
    assert "A manual recovery crawl is already active" in workflow
