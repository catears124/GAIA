from __future__ import annotations

import json

import httpx
import pytest

from gaia.collectors import LeverCollector, SchemaPageCollector
from gaia.models import canonical_url


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
    assert result.status == "verified"
    assert result.postings[0].source_mode == "verification"


@pytest.mark.asyncio
async def test_schema_verifier_separates_closed_blocked_and_verified_pages():
    schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Engineer Intern, Summer 2027",
        "url": "https://example.com/jobs/live",
        "identifier": {"value": "live"},
        "hiringOrganization": {"name": "Example"},
        "employmentType": "INTERN",
    }
    html = f'<script type="application/ld+json">{json.dumps(schema)}</script>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gone"):
            return httpx.Response(410)
        if request.url.path.endswith("/blocked"):
            return httpx.Response(403)
        return httpx.Response(200, text=html)

    urls = [
        "https://example.com/jobs/live",
        "https://example.com/jobs/gone?utm_source=tracker",
        "https://example.com/jobs/blocked",
    ]
    collector = SchemaPageCollector("Example", urls)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)

    assert result.error is None
    assert result.status == "partial"
    assert len(result.postings) == 1
    assert result.closed_urls == [canonical_url(urls[1])]
    assert "stale/closed" in (result.note or "")
    assert "access-blocked" in (result.note or "")


@pytest.mark.asyncio
async def test_all_closed_schema_pages_are_stale_not_broken():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    collector = SchemaPageCollector("Example", ["https://example.com/jobs/gone"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)

    assert result.status == "stale"
    assert result.error is None
    assert result.closed_urls


@pytest.mark.asyncio
async def test_lever_uses_structured_date_posted_when_available():
    api_payload = [
        {
            "id": "12345678-1234-1234-1234-123456789abc",
            "text": "Software Engineer Intern, Summer 2027",
            "hostedUrl": "https://jobs.lever.co/example/12345678-1234-1234-1234-123456789abc",
            "categories": {"location": "New York, NY", "commitment": "Intern"},
        }
    ]
    schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Engineer Intern, Summer 2027",
        "url": api_payload[0]["hostedUrl"],
        "datePosted": "2026-07-20T12:00:00Z",
        "employmentType": "INTERN",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co":
            return httpx.Response(200, json=api_payload)
        return httpx.Response(
            200,
            text=f'<script type="application/ld+json">{json.dumps(schema)}</script>',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await LeverCollector("Example", "example").collect(client)
    assert result.postings[0].posted_at is not None
    assert result.postings[0].posted_confidence == "structured"
