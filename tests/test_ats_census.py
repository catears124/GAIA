from __future__ import annotations

from gaia.ats_census import (
    PATTERNS,
    deserialize_collectors,
    extract_collectors,
    parse_cdx,
    serialize_collectors,
)


def _pattern(provider: str):
    return next(item for item in PATTERNS if item.provider == provider)


def test_parse_cdx_keeps_successful_urls() -> None:
    body = "\n".join(
        [
            '{"url":"https://boards.greenhouse.io/acme/jobs/1","status":"200"}',
            '{"url":"https://boards.greenhouse.io/dead/jobs/2","status":"404"}',
            "not-json",
            '{"url":"https://boards.greenhouse.io/other"}',
        ]
    )
    assert parse_cdx(body) == [
        "https://boards.greenhouse.io/acme/jobs/1",
        "https://boards.greenhouse.io/other",
    ]


def test_greenhouse_census_extracts_and_deduplicates_tenants() -> None:
    collectors = extract_collectors(
        [
            "https://boards.greenhouse.io/acme/jobs/1",
            "https://boards.greenhouse.io/acme/jobs/2",
            "https://job-boards.greenhouse.io/beta/jobs/3",
            "https://boards.greenhouse.io/embed/job_app?for=ignored",
        ],
        _pattern("greenhouse"),
    )
    assert sorted(item.name for item in collectors) == [
        "greenhouse:acme",
        "greenhouse:beta",
    ]


def test_workday_census_requires_concrete_tenant_and_site() -> None:
    collectors = extract_collectors(
        [
            "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs/REQ-1",
            "https://acme.wd5.myworkdayjobs.com/External/job/REQ-1",
        ],
        _pattern("workday"),
    )
    assert len(collectors) == 1
    collector = collectors[0]
    assert collector.name == "workday:acme:external"
    assert (
        collector.endpoint
        == "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    )


def test_generic_subdomain_census_uses_existing_domain_kind() -> None:
    collectors = extract_collectors(
        ["https://www.bamboohr.com/careers", "https://acme.bamboohr.com/careers/12"],
        _pattern("bamboohr"),
    )
    assert [item.name for item in collectors] == ["domain:bamboohr:acme"]
    rows = serialize_collectors(collectors)
    assert rows[0]["kind"] == "domain"
    restored = deserialize_collectors(rows)
    assert len(restored) == 1
    assert restored[0].name == "domain:bamboohr:acme"
    assert restored[0].host == "acme.bamboohr.com"


def test_shared_host_path_tenant_becomes_distinct_domain_candidate() -> None:
    collectors = extract_collectors(
        [
            "https://www.careers-page.com/acme",
            "https://www.careers-page.com/acme/job/ABC123",
            "https://www.careers-page.com/job/phantom",
        ],
        _pattern("manatal"),
    )
    assert [item.name for item in collectors] == ["domain:manatal:acme"]
    assert collectors[0].seed_urls == ["https://www.careers-page.com/acme"]


def test_provider_specific_non_tenant_subdomains_are_denied() -> None:
    collectors = extract_collectors(
        [
            "https://feeds.applicantpool.com/site_map_index.xml",
            "https://acme.applicantpool.com/jobs/123",
        ],
        _pattern("applicantpool"),
    )
    assert [item.name for item in collectors] == ["domain:applicantpool:acme"]


def test_expanded_census_has_broad_provider_surface() -> None:
    providers = {item.provider for item in PATTERNS}
    assert len(providers) >= 30
    assert {
        "bamboohr",
        "csod",
        "eightfold",
        "hiringthing",
        "manatal",
        "personio",
        "teamtailor",
        "workday",
    } <= providers


def test_native_census_rows_round_trip_to_candidate_collectors() -> None:
    greenhouse = extract_collectors(
        ["https://boards.greenhouse.io/acme/jobs/1"], _pattern("greenhouse")
    )
    lever = extract_collectors(["https://jobs.lever.co/beta/123"], _pattern("lever"))
    restored = deserialize_collectors(serialize_collectors([*greenhouse, *lever]))
    assert sorted(item.name for item in restored) == ["greenhouse:acme", "lever:beta"]
