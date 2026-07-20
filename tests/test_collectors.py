from __future__ import annotations

import json

import httpx
import pytest

from gaia.collectors import (
    DatabricksIndexCollector,
    GoogleCareersCollector,
    SchemaPageCollector,
    WorkdayCollector,
)


@pytest.mark.asyncio
async def test_google_collector_recovers_summer_2027_and_date():
    search_html = """
    <html><body>
      <a href="/about/careers/applications/jobs/results/120997883141857990-software-engineering-intern-bs-summer-2027">
        Software Engineering Intern, BS, Summer 2027
      </a>
    </body></html>
    """
    schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Engineering Intern, BS, Summer 2027",
        "url": "https://www.google.com/about/careers/applications/jobs/results/120997883141857990-software-engineering-intern-bs-summer-2027",
        "identifier": {"value": "120997883141857990"},
        "hiringOrganization": {"name": "Google"},
        "datePosted": "2026-07-20T12:00:00Z",
        "employmentType": "INTERN",
        "jobLocation": {
            "address": {
                "addressLocality": "Mountain View",
                "addressRegion": "CA",
                "addressCountry": "US",
            }
        },
    }
    detail_html = f'<script type="application/ld+json">{json.dumps(schema)}</script>'

    def handler(request: httpx.Request) -> httpx.Response:
        if "120997883141857990" in str(request.url):
            return httpx.Response(200, text=detail_html)
        page = request.url.params.get("page")
        return httpx.Response(200, text=search_html if page == "1" else "<html></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GoogleCareersCollector(pages=3).collect(client)
    assert result.complete is True
    assert len(result.postings) == 1
    assert result.postings[0].target_match == "exact"
    assert result.postings[0].posted_at is not None


@pytest.mark.asyncio
async def test_databricks_backstop_is_explicitly_non_complete():
    html = """
    <a href="https://www.linkedin.com/jobs/view/123456789">
      Product Management Intern (Summer 2027)
    </a>
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DatabricksIndexCollector().collect(client)
    assert result.complete is False
    assert result.mode == "external-index"
    assert len(result.postings) == 1
    assert result.postings[0].target_match == "exact"


@pytest.mark.asyncio
async def test_workday_enumerates_full_board_and_builds_public_url():
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        offset = int(payload["offset"])
        if offset == 0:
            jobs = [
                {
                    "title": "Software Engineer Intern, Summer 2027",
                    "externalPath": "/job/Austin-TX/Software-Engineer-Intern_R1",
                    "bulletFields": ["R1"],
                    "locationsText": "Austin, TX",
                    "postedOn": "Posted Today",
                },
                {
                    "title": "Senior Accountant",
                    "externalPath": "/job/Austin-TX/Senior-Accountant_R2",
                    "bulletFields": ["R2"],
                    "locationsText": "Austin, TX",
                    "postedOn": "Posted 2 Days Ago",
                },
            ]
        else:
            jobs = []
        return httpx.Response(200, json={"total": 2, "jobPostings": jobs})

    collector = WorkdayCollector(
        "Example",
        "https://example.wd5.myworkdayjobs.com",
        "example",
        "External",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)
    assert result.complete is True
    assert requests[0]["searchText"] == ""
    assert len(result.postings) == 2
    assert result.postings[0].apply_url == (
        "https://example.wd5.myworkdayjobs.com/External/"
        "job/Austin-TX/Software-Engineer-Intern_R1"
    )


@pytest.mark.asyncio
async def test_schema_page_is_verification_not_direct():
    schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Data Science Intern, Summer 2027",
        "url": "https://example.com/jobs/1",
        "identifier": {"value": "1"},
        "hiringOrganization": {"name": "Example"},
        "datePosted": "2026-07-20",
        "employmentType": "INTERN",
    }
    html = f'<script type="application/ld+json">{json.dumps(schema)}</script>'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    collector = SchemaPageCollector("Example", ["https://example.com/jobs/1"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)
    assert result.complete is False
    assert result.postings[0].source_mode == "verification"
