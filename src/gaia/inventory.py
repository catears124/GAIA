from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .collectors import Collector, CollectorResult
from .config import load_sources
from .db import Database
from .discovery import collectors_from_registry, load_universe_seed_postings, registry_collectors
from .market_collectors import WorkdaySearchCollector
from .market_discovery import discover_github_market
from .models import Posting
from .provider_discovery import provider_collectors_from_postings
from .source_catalog import _collector, merge_catalog, save_catalog

LOGGER = logging.getLogger("gaia.inventory")
TARGET_MATCHES = {"exact", "year_confirmed", "source_confirmed"}
ENUMERATOR_MODES = {"board", "board-search", "domain"}


@dataclass(slots=True)
class ClaimedTarget:
    source: str
    kind: str
    scope: str
    spec: dict[str, Any]
    interval_seconds: int
    consecutive_failures: int


@dataclass(slots=True)
class WorkerSummary:
    sources: int = 0
    postings: int = 0
    new: int = 0
    removed: int = 0
    failed: int = 0
    discovery_runs: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "sources": self.sources,
            "postings": self.postings,
            "new": self.new,
            "removed": self.removed,
            "failed": self.failed,
            "discovery_runs": self.discovery_runs,
        }


class InventoryStore:
    def __init__(self, database: Database, worker_id: str) -> None:
        self.database = database
        self.worker_id = worker_id

    @staticmethod
    def _default_interval(kind: str, scope: str) -> int:
        if scope == "historical":
            return 24 * 3600
        return {
            "greenhouse": 15 * 60,
            "lever": 15 * 60,
            "ashby": 15 * 60,
            "smartrecruiters": 20 * 60,
            "recruitee": 20 * 60,
            "workable": 20 * 60,
            "jobvite": 30 * 60,
            "icims": 30 * 60,
            "oracle-cloud": 30 * 60,
            "successfactors": 30 * 60,
            "workday-search": 30 * 60,
            "google-careers": 20 * 60,
            "verification": 60 * 60,
            "domain": 6 * 3600,
        }.get(kind, 60 * 60)

    @staticmethod
    def _priority(kind: str, scope: str) -> int:
        if scope == "historical":
            return 500
        return {
            "greenhouse": 10,
            "lever": 10,
            "ashby": 10,
            "smartrecruiters": 20,
            "recruitee": 20,
            "workable": 20,
            "workday-search": 30,
            "google-careers": 30,
            "jobvite": 40,
            "icims": 40,
            "oracle-cloud": 40,
            "successfactors": 40,
            "verification": 80,
            "domain": 120,
        }.get(kind, 100)

    def sync_catalog(self) -> int:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT source, kind, scope FROM source_catalog ORDER BY source"
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
        return len(payload)

    def ensure_task(self, key: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_tasks(task_key, next_run_at)
                VALUES (%s, now())
                ON CONFLICT(task_key) DO NOTHING
                """,
                (key,),
            )

    def claim_task(self, key: str, *, lease_seconds: int) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE worker_tasks
                SET lease_owner=%s,
                    lease_expires_at=now() + (%s * interval '1 second'),
                    last_started_at=now(),
                    updated_at=now()
                WHERE task_key=%s
                  AND next_run_at<=now()
                  AND (lease_expires_at IS NULL OR lease_expires_at<now())
                RETURNING task_key
                """,
                (self.worker_id, lease_seconds, key),
            ).fetchone()
        return row is not None

    def finish_task(
        self,
        key: str,
        *,
        interval_seconds: int,
        status: str,
        error: str | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE worker_tasks
                SET next_run_at=now() + (%s * interval '1 second'),
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    last_finished_at=now(),
                    last_status=%s,
                    last_error=%s,
                    updated_at=now()
                WHERE task_key=%s AND lease_owner=%s
                """,
                (interval_seconds, status, error, key, self.worker_id),
            )

    def claim_target(self, *, lease_seconds: int) -> ClaimedTarget | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT target.source
                    FROM crawl_targets AS target
                    JOIN source_catalog AS catalog USING(source)
                    WHERE target.enabled
                      AND target.next_run_at<=now()
                      AND (target.lease_expires_at IS NULL OR target.lease_expires_at<now())
                    ORDER BY target.priority, target.next_run_at, target.source
                    FOR UPDATE OF target SKIP LOCKED
                    LIMIT 1
                )
                UPDATE crawl_targets AS target
                SET lease_owner=%s,
                    lease_expires_at=now() + (%s * interval '1 second'),
                    last_started_at=now(),
                    updated_at=now()
                FROM candidate, source_catalog AS catalog
                WHERE target.source=candidate.source
                  AND catalog.source=target.source
                RETURNING target.source, catalog.kind, catalog.scope, catalog.spec,
                          target.interval_seconds, target.consecutive_failures
                """,
                (self.worker_id, lease_seconds),
            ).fetchone()
        if row is None:
            return None
        return ClaimedTarget(
            source=str(row["source"]),
            kind=str(row["kind"]),
            scope=str(row["scope"]),
            spec=dict(row["spec"] or {}),
            interval_seconds=int(row["interval_seconds"]),
            consecutive_failures=int(row["consecutive_failures"] or 0),
        )

    def known_keys(self, source: str) -> tuple[set[str], set[str]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT posting_key, active FROM postings WHERE source=%s", (source,)
            ).fetchall()
        known = {str(row["posting_key"]) for row in rows}
        active = {str(row["posting_key"]) for row in rows if bool(row["active"])}
        return known, active

    def finish_target(
        self,
        target: ClaimedTarget,
        result: CollectorResult,
        *,
        started_at: datetime,
        known_keys: set[str],
        active_keys: set[str],
    ) -> tuple[int, int]:
        current_keys = {
            posting.posting_key
            for posting in result.postings
            if posting.company.strip()
            and posting.title.strip()
            and posting.canonical_apply_url.strip()
        }
        new_keys = current_keys - known_keys
        removed_keys = active_keys - current_keys if result.complete else set()
        finished_at = datetime.now(UTC)

        if result.error:
            self.database.record_failure(result)
        else:
            self.database.apply_result(result, rebuild=False)

        if current_keys or removed_keys:
            with self.database.connect() as connection:
                if current_keys:
                    connection.execute(
                        "UPDATE postings SET removed_at=NULL WHERE posting_key = ANY(%s)",
                        (sorted(current_keys),),
                    )
                if removed_keys:
                    connection.execute(
                        """
                        UPDATE postings
                        SET removed_at=COALESCE(removed_at, %s)
                        WHERE posting_key = ANY(%s) AND NOT active
                        """,
                        (finished_at, sorted(removed_keys)),
                    )

        if result.error:
            failures = target.consecutive_failures + 1
            delay = min(6 * 3600, max(5 * 60, target.interval_seconds) * (2 ** min(failures, 5)))
        elif result.status in {"blocked", "truncated", "partial"} or not result.complete:
            failures = target.consecutive_failures + 1
            delay = min(6 * 3600, max(10 * 60, target.interval_seconds))
        elif result.rows_scanned == 0:
            failures = 0
            delay = max(target.interval_seconds, 6 * 3600)
        else:
            failures = 0
            delay = target.interval_seconds

        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_targets
                SET next_run_at=now() + (%s * interval '1 second'),
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    last_finished_at=%s,
                    last_complete_at=CASE WHEN %s THEN %s ELSE last_complete_at END,
                    last_status=%s,
                    last_rows=%s,
                    expected_rows=%s,
                    consecutive_failures=%s,
                    last_error=%s,
                    updated_at=now()
                WHERE source=%s AND lease_owner=%s
                """,
                (
                    delay,
                    finished_at,
                    result.complete and result.error is None,
                    finished_at,
                    result.status,
                    result.rows_scanned,
                    result.expected_rows,
                    failures,
                    result.error,
                    target.source,
                    self.worker_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO source_snapshots(
                    source, worker_id, started_at, finished_at, status, complete,
                    rows_scanned, expected_rows, target_rows, new_rows, removed_rows, error
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    target.source,
                    self.worker_id,
                    started_at,
                    finished_at,
                    result.status,
                    result.complete,
                    result.rows_scanned,
                    result.expected_rows,
                    sum(posting.target_match in TARGET_MATCHES for posting in result.postings),
                    len(new_keys),
                    len(removed_keys),
                    result.error,
                ),
            )
        return len(new_keys), len(removed_keys)

    def release_target(self, target: ClaimedTarget, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_targets
                SET next_run_at=now() + interval '10 minutes',
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    last_finished_at=now(),
                    last_status='broken',
                    consecutive_failures=consecutive_failures + 1,
                    last_error=%s,
                    updated_at=now()
                WHERE source=%s AND lease_owner=%s
                """,
                (error, target.source, self.worker_id),
            )


class InventoryWorker:
    def __init__(self, database: Database, *, concurrency: int = 12) -> None:
        worker_id = os.getenv("GAIA_WORKER_ID") or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self.database = database
        self.store = InventoryStore(database, worker_id)
        self.concurrency = max(1, concurrency)
        self.settings = load_sources()
        self.discovery_interval = max(
            5 * 60, int(os.getenv("GAIA_MARKET_DISCOVERY_INTERVAL", "900"))
        )
        self.universe_interval = max(
            3600, int(os.getenv("GAIA_UNIVERSE_DISCOVERY_INTERVAL", "86400"))
        )
        self.lease_seconds = max(300, int(os.getenv("GAIA_CRAWL_LEASE_SECONDS", "1800")))
        self.summary = WorkerSummary()

    @staticmethod
    def _normalize_result(collector: Collector, result: CollectorResult) -> CollectorResult:
        result.scope = collector.scope
        if result.mode in ENUMERATOR_MODES:
            if not result.complete and result.status == "ok":
                result.status = "truncated"
            elif result.complete and result.rows_scanned == 0:
                result.status = "dormant" if collector.scope == "historical" else "empty"
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
            elif code in {404, 410} and collector.mode in ENUMERATOR_MODES:
                status = "dormant"
                error = None
                note = f"source is no longer active (HTTP {code})"
        return CollectorResult(
            source=collector.name,
            postings=[],
            complete=status == "dormant",
            mode=collector.mode,
            rows_scanned=0,
            error=error,
            status=status,
            scope=collector.scope,
            note=note,
        )

    def _build_collector(self, target: ClaimedTarget) -> Collector | None:
        collector = _collector(target.kind, target.spec)
        if collector is None:
            return None
        collector.scope = target.scope
        if isinstance(collector, WorkdaySearchCollector) and os.getenv(
            "GAIA_WORKDAY_FULL_INVENTORY", "1"
        ) == "1":
            collector.terms = ("",)
            collector.max_per_term = max(
                collector.max_per_term,
                int(os.getenv("GAIA_WORKDAY_MAX_PER_TERM", "20000")),
            )
        return collector

    async def _apply_auxiliary_results(self, results: list[CollectorResult]) -> None:
        for result in results:
            if result.error:
                self.database.record_failure(result)
            else:
                self.database.apply_result(result, rebuild=False)

    async def _refresh_market(self, client: httpx.AsyncClient, *, include_universe: bool) -> None:
        discovery_postings: list[Posting] = []
        collectors = registry_collectors(self.settings)
        registry_results = await asyncio.gather(
            *(collector.collect(client) for collector in collectors),
            return_exceptions=True,
        )
        for collector, outcome in zip(collectors, registry_results, strict=True):
            if isinstance(outcome, Exception):
                result = self._failure_result(collector, outcome)
            else:
                result = outcome
            if result.error:
                self.database.record_failure(result)
            else:
                self.database.apply_result(result, rebuild=False)
                discovery_postings.extend(result.postings)
                discovery_postings.extend(result.discovery_postings)

        market_postings, market_health = await discover_github_market(client, self.settings)
        await self._apply_auxiliary_results(market_health)
        discovery_postings.extend(market_postings)

        if include_universe:
            universe_postings, universe_health = await load_universe_seed_postings(
                client, self.settings
            )
            await self._apply_auxiliary_results(universe_health)
            discovery_postings.extend(universe_postings)

        generated = merge_catalog(
            collectors_from_registry(discovery_postings, self.settings, deep=True),
            provider_collectors_from_postings(discovery_postings),
        )
        save_catalog(self.database, generated)
        self.store.sync_catalog()
        self.database.rebuild_families()
        self.summary.discovery_runs += 1

    async def _run_discovery_if_due(self, client: httpx.AsyncClient) -> bool:
        self.store.ensure_task("market-discovery")
        self.store.ensure_task("universe-discovery")
        universe = self.store.claim_task("universe-discovery", lease_seconds=self.lease_seconds)
        market = universe or self.store.claim_task(
            "market-discovery", lease_seconds=self.lease_seconds
        )
        if not market:
            return False
        key = "universe-discovery" if universe else "market-discovery"
        try:
            await self._refresh_market(client, include_universe=universe)
        except Exception as exc:
            LOGGER.exception("%s failed", key)
            self.store.finish_task(key, interval_seconds=10 * 60, status="broken", error=repr(exc))
        else:
            self.store.finish_task(
                key,
                interval_seconds=self.universe_interval if universe else self.discovery_interval,
                status="ok",
            )
        return True

    async def _run_target(self, client: httpx.AsyncClient, target: ClaimedTarget) -> None:
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

    async def run(self, *, once: bool = False, budget_seconds: float | None = None) -> WorkerSummary:
        self.store.sync_catalog()
        timeout = httpx.Timeout(float(os.getenv("GAIA_HTTP_TIMEOUT", "45")))
        limits = httpx.Limits(max_connections=max(32, self.concurrency * 3), max_keepalive_connections=24)
        headers = {
            "User-Agent": os.getenv(
                "GAIA_USER_AGENT", "GAIA/5.0 continuous-job-inventory (+github.com/catears124/GAIA)"
            ),
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        }
        deadline = time.monotonic() + budget_seconds if budget_seconds else None
        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
        ) as client:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                discovery_ran = await self._run_discovery_if_due(client)
                targets = [
                    target
                    for _ in range(self.concurrency)
                    if (target := self.store.claim_target(lease_seconds=self.lease_seconds)) is not None
                ]
                if targets:
                    await asyncio.gather(*(self._run_target(client, target) for target in targets))
                    self.database.rebuild_families()
                    continue
                if once:
                    if not discovery_ran:
                        break
                    continue
                await asyncio.sleep(5)
        return self.summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaia-inventory")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--budget-seconds", type=float, default=None)
    parser.add_argument(
        "--concurrency", type=int, default=int(os.getenv("GAIA_WORKER_CONCURRENCY", "12"))
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=os.getenv("GAIA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database = Database(migrate=True)
    summary = asyncio.run(
        InventoryWorker(database, concurrency=args.concurrency).run(
            once=args.once,
            budget_seconds=args.budget_seconds,
        )
    )
    print(summary.as_dict())


if __name__ == "__main__":
    main()
