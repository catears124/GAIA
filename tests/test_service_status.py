from __future__ import annotations

import httpx

from gaia.collectors import AshbyCollector, SchemaPageCollector
from gaia.models import CollectorResult, Posting
from gaia.service import SyncService


def test_historical_watch_promotes_when_it_finds_current_target():
    collector = AshbyCollector("Example", "example")
    collector.scope = "historical"
    posting = Posting(
        company="Example",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc",
        source=collector.name,
        source_id="123",
        target_match="exact",
    )
    result = CollectorResult(
        source=collector.name,
        postings=[posting],
        complete=True,
        mode="board",
        rows_scanned=1,
        expected_rows=1,
    )
    normalized = SyncService._normalize_result(collector, result)
    assert normalized.scope == "current"
    assert normalized.status == "ok"


def test_historical_empty_board_is_dormant():
    collector = AshbyCollector("Example", "example")
    collector.scope = "historical"
    result = CollectorResult(
        source=collector.name,
        postings=[],
        complete=True,
        mode="board",
        rows_scanned=0,
        expected_rows=0,
    )
    normalized = SyncService._normalize_result(collector, result)
    assert normalized.scope == "historical"
    assert normalized.status == "dormant"


def test_current_403_is_access_limited_not_broken():
    collector = SchemaPageCollector("Example", ["https://example.com/jobs/1"])
    request = httpx.Request("GET", "https://example.com/jobs/1")
    response = httpx.Response(403, request=request)
    failure = SyncService._failure_result(
        collector,
        httpx.HTTPStatusError("forbidden", request=request, response=response),
    )
    assert failure.status == "blocked"
    assert failure.error is None
    assert failure.scope == "current"


def test_historical_404_board_is_dormant_not_broken():
    collector = AshbyCollector("Example", "example")
    collector.scope = "historical"
    request = httpx.Request("GET", "https://api.ashbyhq.com/posting-api/job-board/example")
    response = httpx.Response(404, request=request)
    failure = SyncService._failure_result(
        collector,
        httpx.HTTPStatusError("missing", request=request, response=response),
    )
    assert failure.status == "dormant"
    assert failure.error is None
    assert failure.scope == "historical"
