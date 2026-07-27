from __future__ import annotations

import json

import httpx
import pytest

from gaia.market_collectors import SitemapDomainCollector, WorkdaySearchCollector
from gaia.models import Posting
from gaia.native_collectors import GoogleInternshipCollector


@pytest.mark.asyncio
async def test_workday_search_never_scans_the_unfiltered_board():
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            payload = json.loads(request.content)
            requests.append(payload)
            assert payload["searchText"] == "2027 intern"
            offset = int(payload["offset"])
            count = 20 if offset == 0 else 1
            jobs = [
                {
                    "title": f"Software Engineer Intern, Summer 2027 — {offset + index}",
                    "externalPath": f"/job/Austin/Software-Intern_R{offset + index}",
                    "bulletFields": [f"R{offset + index}"],
                    "locationsText": "Austin, TX",
                    "postedOn": "Posted Today",
                }
                for index in range(count)
            ]
            return httpx.Response(200, json={"total": 21, "jobPostings": jobs})
        return httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "jobReqId": request.url.path.rsplit("_R", 1)[-1],
                    "jobDescription": "Build production software.",
                    "timeType": "Full time",
                    "postedOn": "Posted Today",
                }
            },
        )

    collector = WorkdaySearchCollector(
        "Example",
        "https://example.wd5.myworkdayjobs.com",
        "example",
        "External",
        terms=("intern",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)

    assert result.complete is True
    assert result.mode == "board-search"
    assert result.rows_scanned == 21
    assert len(result.postings) == 21
    assert {int(payload["offset"]) for payload in requests} == {0, 20}
    assert all(payload["searchText"] for payload in requests)


@pytest.mark.asyncio
async def test_workday_search_deduplicates_terms_by_external_path():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "jobPostings": [
                        {
                            "title": "Software Engineer Intern, Summer 2027",
                            "externalPath": "/job/Austin/Software-Intern_R1",
                            "bulletFields": ["R1"],
                            "locationsText": "Austin, TX",
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"jobPostingInfo": {"jobReqId": "R1"}})

    collector = WorkdaySearchCollector(
        "Example",
        "https://example.wd5.myworkdayjobs.com",
        "example",
        "External",
        terms=("intern", "co-op"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)
    assert len(result.postings) == 1


@pytest.mark.asyncio
async def test_sitemap_domain_enumerates_structured_job_pages():
    robots = "Sitemap: https://careers.example.com/sitemap.xml\n"
    sitemap = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://careers.example.com/jobs/software-intern-2027</loc></url>
      <url><loc>https://careers.example.com/about</loc></url>
    </urlset>
    """
    schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Engineer Intern, Summer 2027",
        "url": "https://careers.example.com/jobs/software-intern-2027",
        "identifier": {"value": "R1"},
        "hiringOrganization": {"name": "Example"},
        "datePosted": "2026-07-20T12:00:00Z",
        "employmentType": "INTERN",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap)
        if request.url.path == "/jobs/software-intern-2027":
            return httpx.Response(
                200,
                text=f'<script type="application/ld+json">{json.dumps(schema)}</script>',
            )
        return httpx.Response(404)

    collector = SitemapDomainCollector("Example", "careers.example.com", [])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)
    assert result.complete is True
    assert result.status == "ok"
    assert len(result.postings) == 1
    assert result.postings[0].posted_at is not None


@pytest.mark.asyncio
async def test_sitemap_domain_recovers_unstructured_summer_2027_job_page():
    robots = "Sitemap: https://careers.example.com/sitemap.xml\n"
    sitemap = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://careers.example.com/jobs/ml-intern-2027</loc></url>
    </urlset>
    """
    detail = """
    <html><head><meta property="og:title" content="Machine Learning Intern"></head>
    <body><h1>Machine Learning Intern</h1>
    <p>Our Summer 2027 internship works on production ML systems.</p></body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap)
        if request.url.path == "/jobs/ml-intern-2027":
            return httpx.Response(200, text=detail)
        return httpx.Response(404)

    collector = SitemapDomainCollector("Example", "careers.example.com", [])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)

    assert result.complete is True
    assert len(result.postings) == 1
    assert result.postings[0].title == "Machine Learning Intern"
    assert result.postings[0].target_match == "exact"


@pytest.mark.asyncio
async def test_google_recovers_embedded_job_urls_without_anchor_markup():
    search_html = r'''<script>window.jobs=["\/about\/careers\/applications\/jobs\/results\/120997883141857990-software-engineering-intern-summer-2027"]</script>'''
    schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Engineering Intern, Summer 2027",
        "url": "https://www.google.com/about/careers/applications/jobs/results/120997883141857990-software-engineering-intern-summer-2027",
        "identifier": {"value": "120997883141857990"},
        "hiringOrganization": {"name": "Google"},
        "datePosted": "2026-07-20T12:00:00Z",
        "employmentType": "INTERN",
    }
    detail = f'<script type="application/ld+json">{json.dumps(schema)}</script>'

    def handler(request: httpx.Request) -> httpx.Response:
        if "120997883141857990" in request.url.path:
            return httpx.Response(200, text=detail)
        page = request.url.params.get("page")
        assert request.url.params.get("q") == "2027 intern"
        return httpx.Response(200, text=search_html if page == "1" else "<html></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GoogleInternshipCollector(pages=3).collect(client)
    assert result.complete is True
    assert len(result.postings) == 1
    assert result.postings[0].target_match == "exact"
    assert result.postings[0].posted_at is not None


@pytest.mark.asyncio
async def test_google_detail_heading_does_not_replace_search_result_title():
    posting = Posting(
        company="Google",
        title="Software Developer Intern Bs Summer 2027",
        apply_url="https://www.google.com/about/careers/applications/jobs/results/123-role",
        source="google-careers",
        source_id="123",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><head><meta property='og:title' content='Correct role'></head>"
            "<body><h1>job details</h1><p>Summer 2027 internship</p></body></html>",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enriched = await GoogleInternshipCollector()._enrich(client, posting)

    assert enriched.title == "Software Developer Intern Bs Summer 2027"
    assert enriched.target_match == "exact"
