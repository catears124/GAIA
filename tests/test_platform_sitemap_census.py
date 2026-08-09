from __future__ import annotations

from gaia.platform_sitemap_census import (
    SOURCES,
    collectors_for,
    deserialize_collectors,
    extract_slugs,
    parse_locs,
    serialize_collectors,
)


def _source(provider: str):
    return next(item for item in SOURCES if item.provider == provider)


def test_parse_locs_is_ordered_deduplicated_and_decodes_entities() -> None:
    body = """
    <urlset>
      <url><loc>https://acme.applytojob.com/apply/1?a=1&amp;b=2</loc></url>
      <url><loc>https://beta.applytojob.com/apply/2</loc></url>
      <url><loc>https://acme.applytojob.com/apply/1?a=1&amp;b=2</loc></url>
    </urlset>
    """
    assert parse_locs(body) == [
        "https://acme.applytojob.com/apply/1?a=1&b=2",
        "https://beta.applytojob.com/apply/2",
    ]


def test_isolvedhire_extracts_tenant_subdomains_and_denies_feed_host() -> None:
    source = _source("isolvedhire")
    assert extract_slugs(
        [
            "https://acme.isolvedhire.com/job_site_map.xml",
            "https://beta.isolvedhire.com/jobs/12",
            "https://feeds.isolvedhire.com/site_map_index.xml",
        ],
        source,
    ) == ["acme", "beta"]


def test_jazzhr_extracts_live_board_slugs() -> None:
    source = _source("jazzhr")
    assert extract_slugs(
        [
            "https://acme.applytojob.com/apply/abc",
            "https://beta.applytojob.com/apply/def",
            "https://www.applytojob.com/",
        ],
        source,
    ) == ["acme", "beta"]


def test_sitemap_candidates_round_trip_through_existing_domain_catalog_kind() -> None:
    source = _source("jazzhr")
    collectors = collectors_for(source, ["acme", "beta"])
    rows = serialize_collectors(collectors)
    assert {row["kind"] for row in rows} == {"domain"}
    restored = deserialize_collectors(rows)
    assert sorted(item.name for item in restored) == [
        "domain:jazzhr:acme",
        "domain:jazzhr:beta",
    ]
