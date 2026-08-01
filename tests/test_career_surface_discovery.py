from __future__ import annotations

import json

import httpx
import pytest

from gaia.career_surface_collector import (
    CareerSurfaceCollector,
    career_seed_urls,
    provider_kind,
)
from gaia.db import Database
from gaia.inventory_runtime import COVERAGE_KINDS, InventoryWorker
from gaia.models import Posting
from gaia.provider_discovery import provider_collectors_from_postings
from gaia.source_catalog import _collector


@pytest.mark.asyncio
async def test_career_surface_recurses_sitemaps_and_emits_provider_evidence() -> None:
    job_url = "https://quiet.example/careers/jobs/123-software-intern"
    schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Engineer Intern",
        "url": job_url,
        "identifier": {"value": "123"},
        "datePosted": "2026-08-01T04:00:00Z",
        "hiringOrganization": {"name": "Quiet Robotics"},
        "jobLocation": {
            "address": {
                "addressLocality": "Birmingham",
                "addressRegion": "AL",
                "addressCountry": "US",
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://quiet.example/robots.txt":
            return httpx.Response(
                200,
                text="Sitemap: https://quiet.example/sitemap-index.xml",
                request=request,
            )
        if url == "https://quiet.example/sitemap-index.xml":
            return httpx.Response(
                200,
                text="""<?xml version="1.0"?>
                <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <sitemap><loc>https://quiet.example/jobs.xml</loc></sitemap>
                </sitemapindex>""",
                request=request,
            )
        if url == "https://quiet.example/jobs.xml":
            return httpx.Response(
                200,
                text=f"""<?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>{job_url}</loc></url>
                </urlset>""",
                request=request,
            )
        if url == "https://quiet.example/":
            return httpx.Response(
                200,
                text='<a href="/careers">Careers</a>',
                request=request,
            )
        if url == "https://quiet.example/careers":
            return httpx.Response(
                200,
                text=(
                    '<a href="/careers/jobs/123-software-intern">Open role</a>'
                    '<a href="https://jobs.ashbyhq.com/quiet-robotics">All jobs</a>'
                ),
                request=request,
            )
        if url == job_url:
            return httpx.Response(
                200,
                text=(
                    '<html><head><script type="application/ld+json">'
                    + json.dumps(schema)
                    + "</script></head><body></body></html>"
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

    assert result.mode == "board-search"
    assert result.complete is True
    assert result.status == "ok"
    assert [posting.title for posting in result.postings] == ["Software Engineer Intern"]
    assert result.postings[0].posted_at is not None
    assert [posting.apply_url for posting in result.discovery_postings] == [
        "https://jobs.ashbyhq.com/quiet-robotics"
    ]
    assert "1 provider links" in str(result.note)


@pytest.mark.asyncio
async def test_generic_homepage_does_not_validate_as_an_empty_career_board() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://quiet.example/":
            return httpx.Response(200, text="<h1>Quiet Robotics</h1>", request=request)
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CareerSurfaceCollector(
            "Quiet Robotics",
            "quiet.example",
            ["https://quiet.example/"],
        ).collect(client)

    assert result.complete is False
    assert result.status == "unstructured"


@pytest.mark.asyncio
async def test_reachable_exhaustive_career_page_can_validate_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://quiet.example/careers":
            return httpx.Response(
                200,
                text="<main><h1>Careers</h1><p>No openings right now.</p></main>",
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
    assert result.status == "empty"
    assert result.postings == []


def test_domain_catalog_reconstructs_recursive_collector() -> None:
    collector = _collector(
        "domain",
        {
            "company": "Quiet Robotics",
            "host": "quiet.example",
            "seed_urls": ["https://quiet.example/careers"],
        },
    )

    assert isinstance(collector, CareerSurfaceCollector)
    assert collector.mode == "board-search"
    assert "domain" in COVERAGE_KINDS


def test_ecosystem_roots_and_unsupported_ats_hosts_create_domain_sources() -> None:
    postings = [
        Posting(
            company="Quiet Robotics",
            title="Employer careers surface",
            apply_url="https://quiet.example/",
            source="ecosystem:yc",
            source_id="quiet",
            source_mode="ecosystem-observation",
        ),
        Posting(
            company="Other Robotics",
            title="Employer careers surface",
            apply_url="https://other.bamboohr.com/careers/12",
            source="ecosystem:directory",
            source_id="other",
            source_mode="verification-lead",
        ),
    ]

    domains = [
        collector
        for collector in provider_collectors_from_postings(postings)
        if isinstance(collector, CareerSurfaceCollector)
    ]

    assert {collector.company for collector in domains} == {
        "Quiet Robotics",
        "Other Robotics",
    }
    assert all(collector.scope == "current" for collector in domains)
    assert provider_kind("https://other.bamboohr.com/careers/12") == "bamboohr"


def test_recursive_discovery_persists_new_provider_candidate(tmp_path) -> None:
    database = Database(tmp_path / "recursive-candidates.db")
    worker = InventoryWorker(database, concurrency=1)
    evidence = Posting(
        company="Quiet Robotics",
        title="Employer careers surface",
        apply_url="https://jobs.ashbyhq.com/quiet-robotics",
        source="domain:quiet.example:Quiet Robotics",
        source_id="ashby",
        source_mode="verification-lead",
    )

    saved = worker._save_recursive_candidates(
        [evidence],
        origin="test-recursive-source",
    )

    with database.connect() as connection:
        rows = connection.execute(
            "SELECT source, kind, origin FROM source_candidates ORDER BY source"
        ).fetchall()
    promoted = next(row for row in rows if row["source"] == "ashby:quiet-robotics")
    assert saved >= 1
    assert promoted["kind"] == "ashby"
    assert promoted["origin"] == "test-recursive-source"


def test_career_seed_generation_preserves_multilevel_public_suffix() -> None:
    seeds = career_seed_urls("research.example.co.uk")

    assert "https://jobs.example.co.uk/" in seeds
    assert "https://careers.example.co.uk/" in seeds
