from __future__ import annotations

import httpx
import pytest

from gaia.collectors import SchemaPageCollector
from gaia.db import Database
from gaia.market_collectors import WorkdaySearchCollector
from gaia.market_discovery import discover_github_market
from gaia.models import Posting
from gaia.source_catalog import load_catalog, merge_catalog, save_catalog


def github_settings() -> dict[str, object]:
    return {
        "market_discovery": {
            "github": {
                "enabled": True,
                "queries": ["2027 internships"],
                "repos_per_query": 5,
                "max_repositories": 5,
                "pushed_within_days": 365,
            }
        }
    }


def github_handler(readme: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/repositories":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "full_name": "community/internships-2027",
                            "default_branch": "main",
                            "pushed_at": "2026-07-20T12:00:00Z",
                            "stargazers_count": 100,
                        }
                    ]
                },
            )
        if request.url.path in {
            "/repos/community/internships-2027/readme",
            "/community/internships-2027/main/README.md",
        }:
            return httpx.Response(200, text=readme)
        raise AssertionError(str(request.url))

    return handler


@pytest.mark.asyncio
async def test_github_market_discovery_finds_live_feed_without_company_names():
    readme = """
    | Company | Role | Location | Apply |
    |---|---|---|---|
    | Example | Software Engineer Intern, Summer 2027 | New York, NY | [Apply](https://jobs.lever.co/example/12345678-1234-1234-1234-123456789abc) |
    """
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_handler(readme))
    ) as client:
        postings, health = await discover_github_market(client, github_settings())
    assert len(postings) == 1
    assert postings[0].company == "Example"
    assert postings[0].source_mode == "external-index"
    assert postings[0].target_match == "exact"
    assert any(result.status == "indexed" for result in health)


@pytest.mark.asyncio
async def test_dynamic_feed_cannot_infer_missing_2027_year():
    readme = """
    | Company | Role | Location | Apply |
    |---|---|---|---|
    | Example | Software Engineer Intern, Summer | New York, NY | [Apply](https://jobs.lever.co/example/12345678-1234-1234-1234-123456789abc) |
    """
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_handler(readme))
    ) as client:
        postings, _health = await discover_github_market(client, github_settings())
    assert len(postings) == 1
    assert postings[0].target_match == "unknown"


def test_source_catalog_persists_discovered_collectors(tmp_path):
    db = Database(tmp_path / "gaia.db")
    collector = WorkdaySearchCollector(
        "Example",
        "https://example.wd5.myworkdayjobs.com",
        "example",
        "External",
        terms=("intern", "co-op"),
    )
    collector.scope = "historical"
    assert save_catalog(db, [collector], validated=True, origin="test") == 1
    loaded = load_catalog(db)
    assert len(loaded) == 1
    assert loaded[0].name == collector.name
    assert loaded[0].scope == "historical"
    assert isinstance(loaded[0], WorkdaySearchCollector)
    assert loaded[0].terms == ("2027 intern", "2027 co-op")


def test_source_catalog_persists_verification_lead_evidence(tmp_path):
    db = Database(tmp_path / "gaia.db")
    lead = Posting(
        company="Example",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://careers.example.com/jobs/123",
        source="registry:test",
        source_id="123",
        source_mode="registry",
        locations=["New York, NY"],
    )
    collector = SchemaPageCollector(
        "Example",
        name="schema:careers.example.com:Example",
        leads=[lead],
    )

    assert save_catalog(db, [collector], validated=True, origin="test") == 1
    loaded = load_catalog(db)

    assert len(loaded) == 1
    assert isinstance(loaded[0], SchemaPageCollector)
    assert len(loaded[0].leads) == 1
    assert loaded[0].leads[0].title == lead.title
    assert loaded[0].leads[0].locations == lead.locations
    assert loaded[0].urls == [lead.apply_url]


def test_source_catalog_never_restores_aggregators_as_trusted_verification(tmp_path):
    db = Database(tmp_path / "gaia.db")
    lead = Posting(
        company="Example",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://jobright.ai/jobs/example",
        source="registry:test",
        source_id="example",
        source_mode="registry",
    )
    collector = SchemaPageCollector(
        "Example",
        [lead.apply_url],
        name="schema:jobright.ai:Example",
        leads=[lead],
        trusted=True,
    )

    save_catalog(db, [collector], validated=True, origin="test")
    loaded = load_catalog(db)

    assert len(loaded) == 1
    assert loaded[0].source_mode == "verification-lead"


def test_current_discovery_overrides_historical_catalog_copy():
    historical = WorkdaySearchCollector(
        "Example",
        "https://example.wd5.myworkdayjobs.com",
        "example",
        "External",
    )
    historical.scope = "historical"
    current = WorkdaySearchCollector(
        "Example",
        "https://example.wd5.myworkdayjobs.com",
        "example",
        "External",
    )
    current.scope = "current"
    merged = merge_catalog([current], [historical])
    assert len(merged) == 1
    assert merged[0].scope == "current"
