from __future__ import annotations

import httpx
import pytest

from gaia.models import Posting
from gaia.provider_discovery import provider_collectors_from_postings
from gaia.rippling_collector import RipplingCollector


def test_rippling_job_urls_register_one_board_and_prefer_current_scope():
    postings = [
        Posting(
            company="Example",
            title="Software Engineer Intern 2026",
            apply_url="https://ats.rippling.com/en-US/example-careers/jobs/11111111-1111-1111-1111-111111111111",
            source="universe-seed:test",
            source_id="old",
            source_mode="universe-seed",
        ),
        Posting(
            company="Example Inc",
            title="Software Engineer Intern 2027",
            apply_url="https://ats.rippling.com/example-careers/jobs/22222222-2222-2222-2222-222222222222/apply",
            source="registry:test",
            source_id="current",
            source_mode="registry",
        ),
    ]

    boards = [
        collector
        for collector in provider_collectors_from_postings(postings)
        if isinstance(collector, RipplingCollector)
    ]
    assert len(boards) == 1
    assert boards[0].name == "rippling:example-careers"
    assert boards[0].company == "Example Inc"
    assert boards[0].scope == "current"


@pytest.mark.asyncio
async def test_rippling_collector_keeps_good_jobs_when_one_detail_disappears():
    good_id = "11111111-1111-1111-1111-111111111111"
    gone_id = "22222222-2222-2222-2222-222222222222"
    listing = f"""
    <a href="/example/jobs/{good_id}">Good</a>
    <a href="/example/jobs/{gone_id}">Gone</a>
    <a href="/example/jobs/{good_id}/apply">Duplicate apply link</a>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in {"/example/jobs", "/en-US/example/jobs"}:
            return httpx.Response(200, text=listing, request=request)
        if path.endswith(good_id):
            return httpx.Response(
                200,
                text="<html><body><main><h1>Software Engineer Intern</h1><p>Remote</p></main></body></html>",
                request=request,
            )
        return httpx.Response(404, text="withdrawn", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await RipplingCollector("Example", "example").collect(client)

    assert [posting.title for posting in result.postings] == ["Software Engineer Intern"]
    assert result.rows_scanned == 2
    assert result.expected_rows == 2
    assert result.complete is False
    assert result.status == "partial"
