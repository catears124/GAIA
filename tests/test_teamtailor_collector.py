from __future__ import annotations

import httpx
import pytest

from gaia.models import Posting
from gaia.provider_discovery import provider_collectors_from_postings
from gaia.teamtailor_collector import TeamtailorCollector


def test_teamtailor_urls_register_one_board_and_prefer_current_scope():
    postings = [
        Posting(
            company="Example",
            title="Software Engineer Intern 2026",
            apply_url="https://example.teamtailor.com/jobs/123-old-role",
            source="universe-seed:test",
            source_id="old",
            source_mode="universe-seed",
        ),
        Posting(
            company="Example Inc",
            title="Software Engineer Intern 2027",
            apply_url="https://example.teamtailor.com/jobs/456-new-role",
            source="registry:test",
            source_id="current",
            source_mode="registry",
        ),
    ]

    boards = [
        collector
        for collector in provider_collectors_from_postings(postings)
        if isinstance(collector, TeamtailorCollector)
    ]
    assert len(boards) == 1
    assert boards[0].name == "teamtailor:example"
    assert boards[0].company == "Example Inc"
    assert boards[0].scope == "current"


@pytest.mark.asyncio
async def test_teamtailor_collector_paginates_deduplicates_and_keeps_good_jobs():
    first = """
    <a href="/jobs/101-software-engineer-intern">First</a>
    <a href="https://other.teamtailor.com/jobs/999-wrong-board">Other company</a>
    """
    second = """
    <a href="/jobs/101-software-engineer-intern">Duplicate</a>
    <a href="/jobs/202-data-intern">Second</a>
    """
    repeated = second

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs":
            page = request.url.params.get("page")
            if page == "2":
                return httpx.Response(200, text=second, request=request)
            if page == "3":
                return httpx.Response(200, text=repeated, request=request)
            return httpx.Response(200, text=first, request=request)
        if request.url.path.startswith("/jobs/101-"):
            return httpx.Response(
                200,
                text="<html><body><main><h1>Software Engineer Intern</h1><p>Remote</p></main></body></html>",
                request=request,
            )
        return httpx.Response(404, text="withdrawn", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await TeamtailorCollector("Example", "example").collect(client)

    assert [posting.title for posting in result.postings] == ["Software Engineer Intern"]
    assert result.rows_scanned == 2
    assert result.expected_rows == 2
    assert result.complete is False
    assert result.status == "partial"
