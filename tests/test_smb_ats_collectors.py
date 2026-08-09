from __future__ import annotations

import json

import httpx
import pytest

from gaia.smb_ats_collectors import (
    ISolvedHireCollector,
    JazzHRTargetCollector,
    extract_isolved_domain_id,
    parse_isolved_jobs,
    parse_jazz_target_links,
)


def test_extract_isolved_domain_id() -> None:
    html = '<script>courierCurrentRouteData = {"domain_id":"12345","x":1};</script>'
    assert extract_isolved_domain_id(html) == "12345"
    assert extract_isolved_domain_id("<html></html>") is None


def test_parse_isolved_jobs_classifies_target() -> None:
    payload = {
        "success": True,
        "data": {
            "jobs": [
                {
                    "id": 7,
                    "title": "Software Engineering Intern - Summer 2027",
                    "city": "Birmingham",
                    "abbreviation": "AL",
                    "employmentType": "Intern",
                    "workplaceType": "Hybrid",
                    "startDateRef": "Aug 09, 2026",
                    "jobUrl": "https://acme.isolvedhire.com/jobs/7",
                }
            ]
        },
    }
    postings = parse_isolved_jobs(
        payload, slug="acme", company="Acme", source="isolvedhire:acme"
    )
    assert len(postings) == 1
    posting = postings[0]
    assert posting.target_match == "exact"
    assert posting.category == "software"
    assert posting.locations == ["Birmingham, AL"]
    assert posting.source_mode == "direct"


def test_parse_jazz_target_links_prefilters_internship_slugs() -> None:
    html = """
      <a href="https://acme.applytojob.com/apply/ABCD1234/Software-Engineering-Intern-Summer-2027">Intern</a>
      <a href="https://acme.applytojob.com/apply/EFGH5678/Senior-Software-Engineer">Senior</a>
      <a href="https://other.applytojob.com/apply/IJKL9999/Data-Intern">Other tenant</a>
    """
    assert parse_jazz_target_links(html, slug="acme") == [
        (
            "ABCD1234",
            "https://acme.applytojob.com/apply/ABCD1234/Software-Engineering-Intern-Summer-2027",
        )
    ]


@pytest.mark.asyncio
async def test_isolved_collector_uses_two_step_public_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs/":
            return httpx.Response(
                200,
                text='<script>courierCurrentRouteData = {"domain_id":321};</script>',
            )
        assert request.url.path == "/core/jobs/321"
        assert request.url.params.get("getParams") == '{"isInternal":0}'
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "jobs": [
                        {
                            "id": "one",
                            "title": "Machine Learning Intern Summer 2027",
                            "jobUrl": "https://acme.isolvedhire.com/jobs/one",
                        }
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ISolvedHireCollector("acme", "Acme").collect(client)
    assert result.complete is True
    assert result.status == "loaded"
    assert len(result.postings) == 1
    assert result.postings[0].target_match == "exact"


@pytest.mark.asyncio
async def test_jazz_target_collector_reads_only_candidate_detail_pages() -> None:
    detail_schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Data Engineering Intern - Summer 2027",
        "url": "https://acme.applytojob.com/apply/ABCD1234/Data-Engineering-Intern-Summer-2027",
        "identifier": {"value": "ABCD1234"},
        "hiringOrganization": {"name": "Acme"},
        "datePosted": "2026-08-09",
        "jobLocation": {"address": {"addressLocality": "New York", "addressRegion": "NY"}},
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/apply/":
            return httpx.Response(
                200,
                text=(
                    '<a href="https://acme.applytojob.com/apply/ABCD1234/'
                    'Data-Engineering-Intern-Summer-2027">Intern</a>'
                    '<a href="https://acme.applytojob.com/apply/ZZZZ9999/'
                    'Senior-Engineer">Senior</a>'
                ),
            )
        return httpx.Response(
            200,
            text=f'<script type="application/ld+json">{json.dumps(detail_schema)}</script>',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await JazzHRTargetCollector("acme", "Acme").collect(client)
    assert result.complete is True
    assert len(result.postings) == 1
    assert result.postings[0].target_match == "exact"
    assert "/apply/ZZZZ9999/Senior-Engineer" not in seen
