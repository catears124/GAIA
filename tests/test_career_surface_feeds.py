from __future__ import annotations

import json

import httpx
import pytest

from gaia.career_surface_collector import CareerSurfaceCollector, provider_kind


@pytest.mark.asyncio
async def test_career_rss_feed_discovers_job_detail() -> None:
    job_url = "https://quiet.example/careers/jobs/900-software-intern"
    schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Engineering Intern",
        "url": job_url,
        "identifier": {"value": "900"},
        "datePosted": "2026-08-01T05:00:00Z",
        "hiringOrganization": {"name": "Quiet Robotics"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://quiet.example/careers":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    '<h1>Careers</h1><link rel="alternate" '
                    'type="application/rss+xml" title="Jobs feed" '
                    'href="/careers/jobs.xml">'
                ),
                request=request,
            )
        if url == "https://quiet.example/careers/jobs.xml":
            return httpx.Response(
                200,
                headers={"content-type": "application/rss+xml"},
                text=(
                    "<?xml version='1.0'?><rss><channel><item>"
                    "<title>Software Engineering Intern</title>"
                    f"<link>{job_url}</link>"
                    "</item></channel></rss>"
                ),
                request=request,
            )
        if url == job_url:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    '<script type="application/ld+json">'
                    + json.dumps(schema)
                    + "</script>"
                ),
                request=request,
            )
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CareerSurfaceCollector(
            "Quiet Robotics",
            "quiet.example",
            ["https://quiet.example/careers"],
        ).collect(client)

    assert result.complete is True
    assert result.status == "ok"
    assert [posting.apply_url for posting in result.postings] == [job_url]
    assert "1 feeds" in str(result.note)


@pytest.mark.asyncio
async def test_generic_sitemap_does_not_validate_empty_career_board() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://quiet.example/":
            return httpx.Response(200, text="<h1>Quiet Robotics</h1>", request=request)
        if url == "https://quiet.example/sitemap.xml":
            return httpx.Response(
                200,
                headers={"content-type": "application/xml"},
                text=(
                    "<?xml version='1.0'?>"
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc>https://quiet.example/about</loc></url>"
                    "</urlset>"
                ),
                request=request,
            )
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CareerSurfaceCollector(
            "Quiet Robotics",
            "quiet.example",
            ["https://quiet.example/"],
        ).collect(client)

    assert result.complete is False
    assert result.status == "unstructured"
    assert "1 sitemaps" in str(result.note)
    assert "0 career sitemap URLs" in str(result.note)


def test_provider_fingerprint_requires_domain_boundary() -> None:
    assert provider_kind("https://jobs.lever.co/example") == "lever"
    assert provider_kind("https://notlever.co/jobs/example") is None
    assert provider_kind("https://lever.co.example/jobs/example") is None
