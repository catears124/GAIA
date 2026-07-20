from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .collectors import Collector, CollectorResult
from .config import load_sources
from .db import Database
from .discovery import (
    collectors_from_registry,
    load_universe_seed_postings,
    registry_collectors,
)
from .models import Posting

LOGGER = logging.getLogger("gaia")


@dataclass(slots=True)
class SyncSummary:
    sources: int = 0
    postings: int = 0
    failed: int = 0
    universe_seeds: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "sources": self.sources,
            "postings": self.postings,
            "failed": self.failed,
            "universe_seeds": self.universe_seeds,
        }


class SyncService:
    def __init__(self, db: Database, *, concurrency: int = 16) -> None:
        self.db = db
        self.concurrency = concurrency
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[SyncSummary] | None = None
        self.last_summary: SyncSummary | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start_background(self) -> bool:
        if self.running:
            return False
        self._task = asyncio.create_task(self.sync())
        return True

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def sync(self) -> SyncSummary:
        async with self._lock:
            run_id = self.db.start_run()
            summary = SyncSummary()
            settings = load_sources()
            headers = {
                "User-Agent": os.getenv(
                    "GAIA_USER_AGENT",
                    "GAIA/1.0 internship-research (+github.com/catears124/GAIA)",
                ),
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            }
            timeout = httpx.Timeout(float(os.getenv("GAIA_HTTP_TIMEOUT", "30")))
            limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
            try:
                async with httpx.AsyncClient(
                    headers=headers,
                    timeout=timeout,
                    limits=limits,
                    follow_redirects=True,
                ) as client:
                    registry_results = await self._run_collectors(
                        registry_collectors(settings), client, summary
                    )
                    registry_postings: list[Posting] = [
                        posting
                        for result in registry_results
                        if result.error is None
                        for posting in result.postings
                    ]
                    universe_seeds, seed_health = await load_universe_seed_postings(
                        client, settings
                    )
                    summary.universe_seeds = len(universe_seeds)
                    self._record_seed_health(seed_health, summary)
                    direct_collectors = collectors_from_registry(
                        [*registry_postings, *universe_seeds], settings
                    )
                    await self._run_collectors(direct_collectors, client, summary)
            except asyncio.CancelledError:
                self.db.finish_run(
                    run_id,
                    sources=summary.sources,
                    postings=summary.postings,
                    failed=summary.failed + 1,
                )
                raise
            self.db.finish_run(
                run_id,
                sources=summary.sources,
                postings=summary.postings,
                failed=summary.failed,
            )
            self.last_summary = summary
            return summary

    def _record_seed_health(
        self,
        results: list[CollectorResult],
        summary: SyncSummary,
    ) -> None:
        for result in results:
            if result.error:
                self.db.record_failure(result)
            else:
                self.db.apply_result(result)
        summary.sources += len(results)
        summary.failed += sum(result.error is not None for result in results)

    async def _run_collectors(
        self,
        collectors: list[Collector],
        client: httpx.AsyncClient,
        summary: SyncSummary,
    ) -> list[CollectorResult]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run(collector: Collector) -> CollectorResult:
            async with semaphore:
                try:
                    result = await collector.collect(client)
                    self.db.apply_result(result)
                    LOGGER.info(
                        "%s: scanned=%s targets=%s complete=%s",
                        collector.name,
                        result.rows_scanned,
                        len(result.postings),
                        result.complete,
                    )
                    return result
                except Exception as exc:  # collector isolation is intentional
                    result = CollectorResult(
                        source=collector.name,
                        postings=[],
                        complete=False,
                        mode=collector.mode,
                        rows_scanned=0,
                        error=repr(exc),
                    )
                    self.db.record_failure(result)
                    LOGGER.exception("collector %s failed", collector.name)
                    return result

        results = await asyncio.gather(*(run(collector) for collector in collectors))
        summary.sources += len(results)
        summary.postings += sum(len(result.postings) for result in results)
        summary.failed += sum(result.error is not None for result in results)
        return results

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "last_summary": self.last_summary.as_dict() if self.last_summary else None,
        }
