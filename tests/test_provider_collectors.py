from __future__ import annotations

import httpx
import pytest

from gaia.models import Posting
from gaia.provider_collectors import RecruiteeCollector, SmartRecruitersCollector, WorkableCollector
from gaia.provider_discovery import provider_collectors_from_postings


def test_urls_promote_into_supported_provider_boards():
    postings = [
        Posting(
            company="Smart Example",
            title="Software Intern",
            apply_url="https://jobs.smartrecruiters.com/SmartExample/123-role",
            source="registry:test",
            source_id="1",
            source_mode="registry",
        ),
        Posting(
            company="Recruit Example",
            title="Software Intern",
            apply_url="https://recruit-example.recruitee.com/o/software-intern",
            source="registry:test",
            source_id="2",
            source_mode="registry",
        ),
        Posting(
            company="Work Example",
            title="Software Intern",
            apply_url="https://apply.workable.com/work-example/j/ABC123/",
            source="registry:test",
            source_id="3",
            source_mode="registry",
        ),
    ]
    collectors = provider_collectors_from_postings(postings)
    assert {collector.name for collector in collectors} == {
        "smartrecruiters:SmartExample",
        "recruitee:recruit-example",
        "workable:work-example",
    }
    assert all(collector.scope == "current" for collector in collectors)


@pytest.mark.asyncio
async def test_smartrecruiters_paginates_and_preserves_release_date():
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        offsets.append(offset)
        if offset == 0:
            rows = [
                {
                    "id": str(index),
                    "name": "Software Engineer Intern, Summer 2027",
                    "releasedDate": "2026-07-20T12:00:00Z",
                    "location": {"city": "New York", "region": "NY", "country": "US"},
                }
                for index in range(100)
            ]
        else:
            rows = [
                {
                    "id": "100",
                    "name": "Software Engineer Intern, Summer 2027",
                    "releasedDate": "2026-07-20T12:00:00Z",
                }
            ]
        return httpx.Response(200, json={"content": rows, "totalFound": 101})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SmartRecruitersCollector("Example", "Example").collect(client)
    assert result.complete is True
    assert offsets == [0, 100]
    assert len(result.postings) == 101
    assert result.postings[0].posted_at is not None


@pytest.mark.asyncio
async def test_recruitee_public_offers_are_complete():
    payload = {
        "offers": [
            {
                "id": 7,
                "title": "Machine Learning Intern, Summer 2027",
                "careers_url": "https://example.recruitee.com/o/ml-intern",
                "published_at": "2026-07-20T12:00:00Z",
                "city": "Boston",
                "country": "US",
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    ) as client:
        result = await RecruiteeCollector("Example", "example").collect(client)
    assert result.complete is True
    assert result.postings[0].category == "ml-ai"
    assert result.postings[0].posted_at is not None


@pytest.mark.asyncio
async def test_workable_public_account_jobs_are_complete():
    payload = {
        "jobs": [
            {
                "shortcode": "ABC123",
                "title": "Security Engineer Intern, Summer 2027",
                "url": "https://apply.workable.com/example/j/ABC123/",
                "published_at": "2026-07-20T12:00:00Z",
                "location": {"city": "Austin", "region": "TX", "country": "US"},
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    ) as client:
        result = await WorkableCollector("Example", "example").collect(client)
    assert result.complete is True
    assert result.postings[0].category == "security"
    assert result.postings[0].posted_at is not None
