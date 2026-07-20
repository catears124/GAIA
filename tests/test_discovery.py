from __future__ import annotations

import httpx
import pytest

from gaia.discovery import (
    _choose_apply_url,
    _document_postings,
    _workday_site,
    load_universe_seed_postings,
)


def test_application_link_beats_company_and_aggregator_links():
    urls = [
        "https://example.com",
        "https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc/application",
        "https://simplify.jobs/p/duplicate",
    ]
    assert _choose_apply_url(urls) == urls[1]


def test_simplify_html_table_seeds_employer_board_not_simplify_copy():
    html = """
    <table><tbody><tr>
      <td><a href="https://example.com">Example</a></td>
      <td>Software Engineer Intern</td>
      <td>New York, NY</td>
      <td>
        <a href="https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc/application">Apply</a>
        <a href="https://simplify.jobs/p/copy">Simplify</a>
      </td>
    </tr></tbody></table>
    """
    postings = _document_postings(html, source="seed:test", registry=False)
    assert len(postings) == 1
    assert postings[0].company == "Example"
    assert postings[0].apply_url.startswith("https://jobs.ashbyhq.com/example/")
    assert postings[0].source_mode == "universe-seed"


def test_workday_site_skips_locale_segment():
    assert _workday_site(["en-US", "NVIDIAExternalCareerSite", "job", "Austin", "Role_R1"]) == (
        "NVIDIAExternalCareerSite"
    )
    assert _workday_site(["External", "job", "Austin", "Role_R1"]) == "External"


@pytest.mark.asyncio
async def test_universe_seed_failures_are_reported():
    valid = "https://raw.example/valid.md"
    missing = "https://raw.example/missing.md"
    html = """
    | Company | Role | Location | Apply |
    |---|---|---|---|
    | Example | Software Engineer Intern | NYC | [Apply](https://jobs.lever.co/example/123) |
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == valid:
            return httpx.Response(200, text=html)
        return httpx.Response(404, text="missing")

    settings = {"universe_seeds": [valid, missing]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        postings, health = await load_universe_seed_postings(client, settings)
    assert len(postings) == 1
    assert len(health) == 2
    assert health[0].complete is True
    assert health[0].rows_scanned == 1
    assert health[1].complete is False
    assert health[1].error
