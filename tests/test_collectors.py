from __future__ import annotations

import json

import httpx
import pytest

from gaia.collectors import SchemaPageCollector
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
