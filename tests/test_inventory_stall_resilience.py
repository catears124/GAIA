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


def test_inventory_lanes_stay_warm_without_global_serialization() -> None:
    workflow = Path(".github/workflows/inventory.yml").read_text(encoding="utf-8")
    assert 'cron: "5,35 * * * *"' in workflow
    assert "group: gaia-production-inventory-${{ matrix.lane }}" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'workflow_dispatch' }}" in workflow
    assert '*) budget="3000" ;;' in workflow
    assert "while (( SECONDS < deadline ))" in workflow
    assert "Provider lane failed three consecutive worker slices." in workflow
    assert "needs: prepare" not in workflow


def test_read_models_reconcile_independently_every_ten_minutes() -> None:
    workflow = Path(".github/workflows/reconcile.yml").read_text(encoding="utf-8")
    assert 'cron: "8,18,28,38,48,58 * * * *"' in workflow
    assert "group: gaia-production-reconcile" in workflow
    assert "run: gaia reconcile" in workflow
    assert "run: gaia check --output production-health.json" in workflow


def test_health_recovery_replaces_only_scheduled_lanes() -> None:
    workflow = Path(".github/workflows/production-health.yml").read_text(encoding="utf-8")
    assert '.event == "workflow_dispatch"' in workflow
    assert "gh workflow run inventory.yml --ref main -f budget_seconds=3000" in workflow
    assert "A manual recovery crawl is already active" in workflow
