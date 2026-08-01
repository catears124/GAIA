from __future__ import annotations

import httpx
import pytest

from gaia.db import Database
from gaia.inventory import InventoryWorker as BaseInventoryWorker
from gaia.inventory_runtime import (
    CANDIDATE_PROBE_TASK,
    MARKET_DISCOVERY_TASK,
    InventoryWorker,
)


def _add_due_coverage_source(database: Database) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO source_catalog(source, kind, scope, spec, validated, origin)
            VALUES (
                'greenhouse:busy', 'greenhouse', 'current',
                '{"company":"Busy","board":"busy"}'::jsonb, TRUE, 'test'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO crawl_targets(
                source, enabled, scheduled, priority, interval_seconds, next_run_at
            ) VALUES (
                'greenhouse:busy', TRUE, TRUE, 10, 900,
                now() - interval '10 minutes'
            )
            """
        )


def _add_due_candidate(database: Database) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO source_candidates(
                source, kind, scope, spec, origin, next_probe_at
            ) VALUES (
                'ashby:new-employer', 'ashby', 'current',
                '{"company":"New Employer","board":"new-employer"}'::jsonb,
                'test', now() - interval '10 minutes'
            )
            """
        )


@pytest.mark.asyncio
async def test_due_candidate_probe_runs_even_while_coverage_is_due(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "candidate-reserve.db")
    _add_due_coverage_source(database)
    _add_due_candidate(database)
    worker = InventoryWorker(database, concurrency=1)
    worker.store.ensure_task(CANDIDATE_PROBE_TASK)
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE worker_tasks
            SET next_run_at=now() - interval '10 minutes'
            WHERE task_key=%s
            """,
            (CANDIDATE_PROBE_TASK,),
        )

    probed: list[str] = []

    async def fake_probe(_client: httpx.AsyncClient, target) -> bool:
        probed.append(target.source)
        worker.store.finish_candidate(
            target,
            promoted=True,
            status="ok",
            error=None,
        )
        return True

    monkeypatch.setattr(worker, "_probe_candidate", fake_probe)
    monkeypatch.setenv("GAIA_CANDIDATE_PROBE_LIMIT", "1")

    async with httpx.AsyncClient() as client:
        ran = await worker._run_discovery_if_due(client)

    with database.connect() as connection:
        candidate_count = connection.execute(
            "SELECT COUNT(*) AS count FROM source_candidates"
        ).fetchone()["count"]
        task = connection.execute(
            """
            SELECT last_status,
                   EXTRACT(EPOCH FROM (next_run_at - now())) AS next_seconds
            FROM worker_tasks
            WHERE task_key=%s
            """,
            (CANDIDATE_PROBE_TASK,),
        ).fetchone()

    assert ran is True
    assert probed == ["ashby:new-employer"]
    assert candidate_count == 0
    assert task["last_status"] == "ok"
    assert 0 < float(task["next_seconds"]) <= 70
    assert worker.summary.discovery_runs == 1


@pytest.mark.asyncio
async def test_fresh_coverage_backlog_temporarily_defers_market_discovery(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "market-grace.db")
    _add_due_coverage_source(database)
    worker = InventoryWorker(database, concurrency=1)
    worker.store.ensure_task(MARKET_DISCOVERY_TASK)
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE worker_tasks
            SET next_run_at=now() - interval '10 minutes'
            WHERE task_key=%s
            """,
            (MARKET_DISCOVERY_TASK,),
        )

    async def no_candidate_task(_client: httpx.AsyncClient) -> bool:
        return False

    base_calls: list[bool] = []

    async def fake_base(_worker, _client: httpx.AsyncClient) -> bool:
        base_calls.append(True)
        return True

    monkeypatch.setattr(worker, "_run_candidate_probe_if_due", no_candidate_task)
    monkeypatch.setattr(BaseInventoryWorker, "_run_discovery_if_due", fake_base)

    async with httpx.AsyncClient() as client:
        ran = await worker._run_discovery_if_due(client)

    assert ran is False
    assert base_calls == []


@pytest.mark.asyncio
async def test_starved_market_discovery_preempts_permanent_coverage_backlog(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "market-starvation.db")
    _add_due_coverage_source(database)
    worker = InventoryWorker(database, concurrency=1)
    worker.store.ensure_task(MARKET_DISCOVERY_TASK)
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE worker_tasks
            SET next_run_at=now() - interval '45 minutes'
            WHERE task_key=%s
            """,
            (MARKET_DISCOVERY_TASK,),
        )

    async def no_candidate_task(_client: httpx.AsyncClient) -> bool:
        return False

    base_calls: list[bool] = []

    async def fake_base(_worker, _client: httpx.AsyncClient) -> bool:
        base_calls.append(True)
        return True

    monkeypatch.setattr(worker, "_run_candidate_probe_if_due", no_candidate_task)
    monkeypatch.setattr(BaseInventoryWorker, "_run_discovery_if_due", fake_base)

    async with httpx.AsyncClient() as client:
        ran = await worker._run_discovery_if_due(client)

    assert worker.store.has_due_coverage_targets() is True
    assert worker.store.task_is_overdue(
        MARKET_DISCOVERY_TASK,
        delay_seconds=worker.discovery_starvation_seconds,
    ) is True
    assert ran is True
    assert base_calls == [True]
