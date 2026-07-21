from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .collectors import Collector, CollectorResult
from .config import load_sources
from .db import Database
from .discovery import collectors_from_registry, load_universe_seed_postings, registry_collectors
from .market_discovery import discover_github_market
from .models import Posting
from .native_collectors import GoogleInternshipCollector
from .source_catalog import load_catalog, merge_catalog, save_catalog

LOGGER = logging.getLogger("gaia")
logging.getLogger("httpx").setLevel(logging.WARNING)
TARGET_MATCHES = {"exact", "year_confirmed", "source_confirmed"}
ENUMERATOR_MODES = {"board", "board-search", "domain"}


@dataclass(slots=True)
class SyncSummary:
    mode: str = "refresh"
    sources: int = 0
    postings: int = 0
    failed: int = 0
    universe_seeds: int = 0
    discovered_feeds: int = 0
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["elapsed_seconds"] = round(self.elapsed_seconds, 1)
        return result


@dataclass(slots=True)
class SyncProgress:
    mode: str = "idle"
    stage: str = "idle"
    completed: int = 0
    total: int = 0
    current: str | None = None
    started_at: float | None = None

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["elapsed_seconds"] = (
            round(time.monotonic() - self.started_at, 1)
            if self.started_at is not None
            else 0.0
        )
        return result


class SyncService:
    def __init__(self, db: Database, *, concurrency: int = 16) -> None:
        self.db = db
        self.concurrency = concurrency
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[SyncSummary] | None = None
        self.last_summary: SyncSummary | None = None
        self.progress = SyncProgress()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start_background(self, mode: str = "refresh") -> bool:
        if self.running:
            return False
        if mode not in {"refresh", "discover"}:
            raise ValueError(f"unknown sync mode: {mode}")
        self._task = asyncio.create_task(self.sync(mode=mode))
        return True

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _install_native_collectors(collectors: list[Collector]) -> list[Collector]:
        output: list[Collector] = []
        for collector in collectors:
            if collector.name == "google-careers":
                native = GoogleInternshipCollector()
                native.scope = collector.scope
                output.append(native)
            else:
                output.append(collector)
        return output

    async def sync(self, *, mode: str = "refresh") -> SyncSummary:
        async with self._lock:
            started = time.monotonic()
            self.progress = SyncProgress(
                mode=mode,
                stage="loading current indexes",
                started_at=started,
            )
            run_id = self.db.start_run()
            summary = SyncSummary(mode=mode)
            settings = load_sources()
            headers = {
                "User-Agent": os.getenv(
                    "GAIA_USER_AGENT",
                    "GAIA/2.0 internship-index (+github.com/catears124/GAIA)",
                ),
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            }
            timeout = httpx.Timeout(float(os.getenv("GAIA_HTTP_TIMEOUT", "30")))
            limits = httpx.Limits(max_connections=48, max_keepalive_connections=24)
            try:
                async with httpx.AsyncClient(
                    headers=headers,
                    timeout=timeout,
                    limits=limits,
                    follow_redirects=True,
                ) as client:
                    registry_results = await self._run_collectors(
                        registry_collectors(settings),
                        client,
                        summary,
                        run_id,
                        stage="loading current indexes",
                    )
                    registry_postings: list[Posting] = []
                    for result in registry_results:
                        if result.error is None:
                            registry_postings.extend(result.postings)
                            registry_postings.extend(result.discovery_postings)

                    universe_seeds: list[Posting] = []
                    if mode == "discover":
                        self.progress.stage = "loading historical employer universe"
                        universe_seeds, seed_health = await load_universe_seed_postings(
                            client,
                            settings,
                        )
                        summary.universe_seeds = len(universe_seeds)
                        self._record_results(seed_health, summary, run_id)

                    market_postings: list[Posting] = []
                    if mode == "discover":
                        self.progress.stage = "discovering live market feeds"
                        market_postings, market_health = await discover_github_market(
                            client,
                            settings,
                        )
                        summary.discovered_feeds = sum(
                            result.status == "indexed" for result in market_health
                        )
                        self._record_results(market_health, summary, run_id)

                    generated_collectors = self._install_native_collectors(
                        collectors_from_registry(
                            [*registry_postings, *universe_seeds, *market_postings],
                            settings,
                            deep=mode == "discover",
                        )
                    )
                    catalog_collectors = load_catalog(self.db.path)
                    if mode == "refresh":
                        generated_collectors = [
                            collector
                            for collector in generated_collectors
                            if collector.scope == "current" and collector.mode != "domain"
                        ]
                        catalog_collectors = [
                            collector
                            for collector in catalog_collectors
                            if collector.scope == "current" and collector.mode != "domain"
                        ]
                    direct_collectors = merge_catalog(
                        generated_collectors,
                        catalog_collectors,
                    )
                    save_catalog(self.db.path, generated_collectors)
                    await self._run_collectors(
                        direct_collectors,
                        client,
                        summary,
                        run_id,
                        stage=(
                            "enumerating employers and sitemaps"
                            if mode == "discover"
                            else "refreshing current internship sources"
                        ),
                    )
                    self.progress.stage = "rebuilding role families"
                    self.db.rebuild_families()
            except asyncio.CancelledError:
                self.db.finish_run(
                    run_id,
                    sources=summary.sources,
                    postings=summary.postings,
                    failed=summary.failed + 1,
                )
                self.progress.stage = "cancelled"
                raise
            except Exception:
                self.db.finish_run(
                    run_id,
                    sources=summary.sources,
                    postings=summary.postings,
                    failed=summary.failed + 1,
                )
                self.progress.stage = "failed"
                LOGGER.exception("sync failed")
                raise

            summary.elapsed_seconds = time.monotonic() - started
            self.db.finish_run(
                run_id,
                sources=summary.sources,
                postings=summary.postings,
                failed=summary.failed,
            )
            self.last_summary = summary
            self.progress = SyncProgress(mode=mode, stage="complete")
            return summary

    def _record_results(
        self,
        results: list[CollectorResult],
        summary: SyncSummary,
        run_id: int,
    ) -> None:
        for result in results:
            if result.error:
                self.db.record_failure(result, run_id=run_id)
            else:
                self.db.apply_result(result, rebuild=False, run_id=run_id)
        summary.sources += len(results)
        summary.postings += sum(len(result.postings) for result in results)
        summary.failed += sum(result.error is not None for result in results)

    @staticmethod
    def _normalize_result(collector: Collector, result: CollectorResult) -> CollectorResult:
        has_current_target = any(
            posting.target_match in TARGET_MATCHES for posting in result.postings
        )
        result.scope = "current" if has_current_target else collector.scope
        if result.mode in ENUMERATOR_MODES:
            if not result.complete:
                result.status = "truncated" if result.status == "ok" else result.status
            elif result.rows_scanned == 0:
                if result.scope == "historical":
                    result.status = "dormant"
                    result.note = result.note or "historical watch currently exposes no matching jobs"
                elif result.mode == "board-search":
                    result.status = "empty"
                    result.note = result.note or "internship query returned zero jobs"
                else:
                    result.status = "empty"
                    result.note = result.note or "current source returned zero jobs"
        return result

    @staticmethod
    def _failure_result(collector: Collector, exc: Exception) -> CollectorResult:
        status = "broken"
        error: str | None = repr(exc)
        note: str | None = None
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code in {401, 403, 429}:
                status = "blocked"
                error = None
                note = f"source denied automated access with HTTP {code}"
            elif collector.scope == "historical" and code in {404, 410}:
                status = "dormant"
                error = None
                note = f"historical source is no longer active (HTTP {code})"
        return CollectorResult(
            source=collector.name,
            postings=[],
            complete=False,
            mode=collector.mode,
            rows_scanned=0,
            error=error,
            status=status,
            scope=collector.scope,
            note=note,
        )

    async def _run_collectors(
        self,
        collectors: list[Collector],
        client: httpx.AsyncClient,
        summary: SyncSummary,
        run_id: int,
        *,
        stage: str,
    ) -> list[CollectorResult]:
        semaphore = asyncio.Semaphore(self.concurrency)
        self.progress.stage = stage
        self.progress.total = len(collectors)
        self.progress.completed = 0
        self.progress.current = None

        async def run(collector: Collector) -> CollectorResult:
            async with semaphore:
                self.progress.current = collector.name
                try:
                    result = self._normalize_result(
                        collector,
                        await collector.collect(client),
                    )
                    self.db.apply_result(result, rebuild=False, run_id=run_id)
                    LOGGER.info(
                        "%s scanned=%s targets=%s status=%s",
                        collector.name,
                        result.rows_scanned,
                        len(result.postings),
                        result.status,
                    )
                    return result
                except Exception as exc:
                    result = self._failure_result(collector, exc)
                    if result.error:
                        self.db.record_failure(result, run_id=run_id)
                        if os.getenv("GAIA_DEBUG_COLLECTORS") == "1":
                            LOGGER.exception("collector %s failed", collector.name)
                        else:
                            LOGGER.error("collector %s failed: %s", collector.name, result.error)
                    else:
                        self.db.apply_result(result, rebuild=False, run_id=run_id)
                        LOGGER.warning("collector %s: %s", collector.name, result.note)
                    return result
                finally:
                    self.progress.completed += 1

        results = await asyncio.gather(*(run(collector) for collector in collectors))
        summary.sources += len(results)
        summary.postings += sum(len(result.postings) for result in results)
        summary.failed += sum(result.error is not None for result in results)
        self.progress.current = None
        return results

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "progress": self.progress.as_dict(),
            "last_summary": self.last_summary.as_dict() if self.last_summary else None,
        }
