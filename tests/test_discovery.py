from __future__ import annotations

import httpx
import pytest

from gaia.discovery import (
    _choose_apply_url,
    _document_postings,
    _workday_site,
    collectors_from_registry,
    load_universe_seed_postings,
)
from gaia.models import Posting


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


def test_markdown_company_link_is_cleaned_before_source_naming():
    body = """
    | Company | Role | Location | Apply |
    |---|---|---|---|
    | [Flipp](https://simplify.jobs/c/Flipp) | Software Engineer Intern, Summer 2027 | Toronto | [Apply](https://boards.greenhouse.io/embed/job_board?for=flipp) |
    """
    postings = _document_postings(body, source="registry:test", registry=True)
    assert postings[0].company == "Flipp"


def test_historical_custom_pages_do_not_become_verification_obligations():
    historical = Posting(
        company="Old Company",
        title="Software Engineer Intern, Summer 2026",
        apply_url="https://careers.example.com/jobs/old-role",
        source="universe-seed:test",
        source_id="old",
        source_mode="universe-seed",
    )
    current = Posting(
        company="Current Company",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://careers.current.example/jobs/current-role",
        source="registry:test",
        source_id="current",
        source_mode="registry",
    )
    collectors = collectors_from_registry(
        [historical, current],
        settings={"release_canaries": {}},
    )
    names = {collector.name for collector in collectors}
    assert not any("careers.example.com" in name for name in names)
    assert any("careers.current.example" in name for name in names)


def test_historical_structured_board_is_retained_as_watch_scope():
    historical = Posting(
        company="Example",
        title="Software Engineer Intern, Summer 2026",
        apply_url="https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc",
        source="universe-seed:test",
        source_id="old",
        source_mode="universe-seed",
    )
    collectors = collectors_from_registry([historical], settings={"release_canaries": {}})
    assert len(collectors) == 1
    assert collectors[0].scope == "historical"
    assert collectors[0].name == "ashby:example"


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
    assert health[0].scope == "historical"
    assert health[0].rows_scanned == 1
    assert health[1].complete is False
    assert health[1].error
