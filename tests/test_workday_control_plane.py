from __future__ import annotations

import asyncio

import httpx
import pytest

from gaia.collectors import SchemaPageCollector
from gaia.market_collectors import SitemapDomainCollector, WorkdaySearchCollector
from gaia.models import Posting
from gaia.service import _refresh_catalog_collector


def test_workday_sanitizes_persisted_broad_terms_and_source_case():
    collector = WorkdaySearchCollector(
        "Generac",
        "https://generac.wd5.myworkdayjobs.com",
        "Generac",
        "External",
        terms=("internship", "student", "university", "campus", "summer", "coop"),
    )

    assert collector.terms == ("2027 intern", "2027 co-op")
    assert collector.name == "workday:generac:external"


def test_refresh_rechecks_productive_verification_sources():
    lead = Posting(
        company="Example",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://careers.example.com/job",
        source="registry:test",
        source_id="job",
        source_mode="registry",
    )
    collector = SchemaPageCollector("Example", [lead.apply_url], leads=[lead])

    assert _refresh_catalog_collector(
        collector,
        workday_names=set(),
        domain_names=set(),
        verification_names={collector.name},
    )
    assert not _refresh_catalog_collector(
        collector,
        workday_names=set(),
        domain_names=set(),
        verification_names=set(),
    )
    domain = SitemapDomainCollector("Example", "careers.example.com", [lead.apply_url])
    assert _refresh_catalog_collector(
        domain,
        workday_names=set(),
        domain_names={domain.name},
        verification_names=set(),
    )


@pytest.mark.asyncio
async def test_workday_retries_429_then_recovers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GAIA_WORKDAY_MIN_INTERVAL", "0")
    monkeypatch.setenv("GAIA_WORKDAY_JITTER", "0")
    monkeypatch.setenv("GAIA_WORKDAY_BACKOFF", "0.1")
    monkeypatch.setenv("GAIA_WORKDAY_RETRIES", "3")
    monkeypatch.setenv("GAIA_WORKDAY_CIRCUIT_THRESHOLD", "99")
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"total": 0, "jobPostings": []})

    collector = WorkdaySearchCollector(
        "Example",
        "https://example.wd5.myworkdayjobs.com",
        "example",
        "External",
        terms=("intern",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)

    assert attempts == 3
    assert result.complete is True
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_workday_persistent_429_is_blocked_not_broken(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GAIA_WORKDAY_MIN_INTERVAL", "0")
    monkeypatch.setenv("GAIA_WORKDAY_JITTER", "0")
    monkeypatch.setenv("GAIA_WORKDAY_BACKOFF", "0.1")
    monkeypatch.setenv("GAIA_WORKDAY_RETRIES", "2")
    monkeypatch.setenv("GAIA_WORKDAY_CIRCUIT_THRESHOLD", "99")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    collector = WorkdaySearchCollector(
        "Example",
        "https://example.wd5.myworkdayjobs.com",
        "example",
        "External",
        terms=("intern",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.collect(client)

    assert result.complete is False
    assert result.status == "blocked"
    assert result.error is None
    assert result.postings == []


@pytest.mark.asyncio
async def test_workday_requests_are_globally_serialized(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GAIA_WORKDAY_GLOBAL_CONCURRENCY", "1")
    monkeypatch.setenv("GAIA_WORKDAY_MIN_INTERVAL", "0")
    monkeypatch.setenv("GAIA_WORKDAY_JITTER", "0")
    active = 0
    maximum_active = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"total": 0, "jobPostings": []})

    first = WorkdaySearchCollector(
        "First",
        "https://first.wd5.myworkdayjobs.com",
        "first",
        "External",
        terms=("intern",),
    )
    second = WorkdaySearchCollector(
        "Second",
        "https://second.wd5.myworkdayjobs.com",
        "second",
        "External",
        terms=("intern",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(first.collect(client), second.collect(client))

    assert maximum_active == 1
