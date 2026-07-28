from __future__ import annotations

from gaia.db import Database
from gaia.market_collectors import SitemapDomainCollector
from gaia.models import CollectorResult, Posting
from gaia.provider_discovery import provider_collectors_from_postings
from gaia.source_catalog import load_catalog, save_catalog


def _historical(company: str, url: str, source_id: str = "old") -> Posting:
    return Posting(
        company=company,
        title="Software Engineer Intern, Summer 2026",
        apply_url=url,
        source="universe-seed:test",
        source_id=source_id,
        source_mode="universe-seed",
    )


def test_prior_year_custom_domain_becomes_historical_sitemap_watch():
    collectors = provider_collectors_from_postings(
        [_historical("Example", "https://careers.example.com/jobs/software-intern-2026")]
    )

    domains = [item for item in collectors if isinstance(item, SitemapDomainCollector)]
    assert len(domains) == 1
    assert domains[0].host == "careers.example.com"
    assert domains[0].scope == "historical"
    assert domains[0].seed_urls == []


def test_prior_year_domain_watch_deduplicates_host_and_uses_dominant_company():
    collectors = provider_collectors_from_postings(
        [
            _historical("Example Inc.", "https://jobs.example.com/jobs/one", "1"),
            _historical("Example", "https://jobs.example.com/jobs/two", "2"),
            _historical("Example", "https://jobs.example.com/jobs/three", "3"),
        ]
    )

    domains = [item for item in collectors if isinstance(item, SitemapDomainCollector)]
    assert len(domains) == 1
    assert domains[0].company == "Example"


def test_prior_year_hosted_ats_is_not_misclassified_as_employer_domain():
    collectors = provider_collectors_from_postings(
        [
            _historical(
                "Example",
                "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/123",
            )
        ]
    )
    assert not any(isinstance(item, SitemapDomainCollector) for item in collectors)


def test_current_custom_page_is_left_to_current_registry_discovery():
    current = Posting(
        company="Example",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://careers.example.com/jobs/software-intern-2027",
        source="registry:test",
        source_id="current",
        source_mode="registry",
    )
    assert provider_collectors_from_postings([current]) == []


def test_historical_catalog_watch_promotes_after_finding_current_role(tmp_path):
    db = Database(tmp_path / "gaia.db")
    collector = SitemapDomainCollector("Example", "careers.example.com", [])
    collector.scope = "historical"
    save_catalog(db, [collector], validated=True, origin="test")

    posting = Posting(
        company="Example",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://careers.example.com/jobs/software-intern-2027",
        source=collector.name,
        source_id="2027-role",
        source_mode="verification",
        category="software",
        season="summer",
        year=2027,
        target_match="exact",
    )
    db.apply_result(
        CollectorResult(
            source=collector.name,
            postings=[posting],
            complete=True,
            mode="domain",
            rows_scanned=1,
            expected_rows=1,
            status="ok",
            scope="current",
        ),
        rebuild=False,
    )

    loaded = load_catalog(db)
    assert len(loaded) == 1
    assert loaded[0].scope == "current"
