from __future__ import annotations

import httpx
import pytest

from gaia.discovery import JsonRegistryCollector, MarkdownRegistryCollector


@pytest.mark.asyncio
async def test_markdown_generic_greenhouse_board_is_discovery_only():
    body = """
    | Company | Role | Location | Apply |
    |---|---|---|---|
    | [Flipp](https://simplify.jobs/c/Flipp) | Software Engineer Intern, Summer 2027 | Toronto | [Apply](https://boards.greenhouse.io/embed/job_board?for=flipp) |
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    collector = MarkdownRegistryCollector("test", "https://registry.example/list.md")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)

    assert result.postings == []
    assert len(result.discovery_postings) == 1
    assert result.discovery_postings[0].company == "Flipp"
    assert result.discovery_postings[0].target_match == "exact"


@pytest.mark.asyncio
async def test_json_generic_ashby_board_is_discovery_only():
    payload = [
        {
            "id": "row-1",
            "company_name": "Example",
            "title": "Software Engineer Intern, Summer 2027",
            "url": "https://jobs.ashbyhq.com/example",
            "locations": ["New York, NY"],
            "active": True,
            "is_visible": True,
        }
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    collector = JsonRegistryCollector("test", "https://registry.example/list.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)

    assert result.postings == []
    assert len(result.discovery_postings) == 1
    assert result.discovery_postings[0].apply_url == "https://jobs.ashbyhq.com/example"


@pytest.mark.asyncio
async def test_specific_greenhouse_application_remains_benchmark_eligible():
    body = """
    | Company | Role | Location | Apply |
    |---|---|---|---|
    | Example | Software Engineer Intern, Summer 2027 | NYC | [Apply](https://job-boards.greenhouse.io/example/jobs/1234567) |
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    collector = MarkdownRegistryCollector("test", "https://registry.example/list.md")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)

    assert len(result.postings) == 1
    assert result.discovery_postings == []
