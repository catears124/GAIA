from __future__ import annotations

from gaia import career_surface_collector as career
from gaia.freshness_coverage_extension import install_freshness_coverage_extension


def _install() -> None:
    install_freshness_coverage_extension()


def test_sitemap_entries_are_prioritized_by_last_modified() -> None:
    _install()

    locations, is_index = career._xml_locations(
        """
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/jobs/old</loc><lastmod>2026-01-01</lastmod></url>
          <url><loc>https://example.com/jobs/today</loc><lastmod>2026-08-01T14:30:00Z</lastmod></url>
          <url><loc>https://example.com/jobs/yesterday</loc><lastmod>2026-07-31</lastmod></url>
        </urlset>
        """
    )

    assert is_index is False
    assert locations == [
        "https://example.com/jobs/today",
        "https://example.com/jobs/yesterday",
        "https://example.com/jobs/old",
    ]


def test_sitemap_entries_without_dates_preserve_document_order_after_dated_rows() -> None:
    _install()

    locations, _ = career._xml_locations(
        """
        <urlset>
          <url><loc>https://example.com/jobs/first-undated</loc></url>
          <url><loc>https://example.com/jobs/recent</loc><lastmod>2026-08-01</lastmod></url>
          <url><loc>https://example.com/jobs/second-undated</loc></url>
        </urlset>
        """
    )

    assert locations == [
        "https://example.com/jobs/recent",
        "https://example.com/jobs/first-undated",
        "https://example.com/jobs/second-undated",
    ]


def test_sitemap_index_children_are_prioritized_by_last_modified() -> None:
    _install()

    locations, is_index = career._xml_locations(
        """
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://example.com/sitemap-2025.xml</loc><lastmod>2025-12-31</lastmod></sitemap>
          <sitemap><loc>https://example.com/sitemap-current.xml</loc><lastmod>2026-08-01</lastmod></sitemap>
        </sitemapindex>
        """
    )

    assert is_index is True
    assert locations[0] == "https://example.com/sitemap-current.xml"


def test_machine_readable_job_feeds_are_seeded() -> None:
    _install()

    seeds = career.career_seed_urls("research.example")

    assert "https://research.example/jobs.json" in seeds
    assert "https://research.example/api/jobs" in seeds
    assert "https://research.example/api/v2/jobs" in seeds
    assert "https://research.example/api/requisitions" in seeds


def test_additional_modern_ats_hosts_are_recognized() -> None:
    _install()

    assert career.provider_kind("https://jobs.dover.com/acme/123") == "dover"
    assert career.provider_kind("https://jobs.gem.com/acme/123") == "gem"
    assert career.provider_kind("https://jobs.polymer.co/acme/123") == "polymer"
    assert career.provider_kind("https://acme.homerun.co/job/123") == "homerun"
    assert career.provider_kind("https://jobs.gusto.com/postings/acme-123") == "gusto"
    assert career.provider_kind("https://acme.recruitingbypaycor.com/career/JobIntroduction.action?id=123") == "paycor"


def test_freshness_extension_is_idempotent() -> None:
    _install()
    first_parser = career._xml_locations
    first_paths = career.CAREER_PATHS

    install_freshness_coverage_extension()

    assert career._xml_locations is first_parser
    assert career.CAREER_PATHS == first_paths
