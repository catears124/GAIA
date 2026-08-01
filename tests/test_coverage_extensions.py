from __future__ import annotations

from gaia.career_surface_collector import career_seed_urls, provider_kind
from gaia.coverage_extensions import install_coverage_extensions


def test_install_expands_early_career_and_search_entry_points() -> None:
    install_coverage_extensions()

    seeds = career_seed_urls("research.example")

    assert "https://research.example/internships" in seeds
    assert "https://research.example/early-careers" in seeds
    assert "https://research.example/university-recruiting" in seeds
    assert "https://research.example/jobs/search" in seeds


def test_install_recognizes_additional_shared_hosted_ats_products() -> None:
    install_coverage_extensions()

    assert provider_kind("https://acme.breezy.hr/p/abc-software-intern") == "breezy"
    assert provider_kind("https://acme.applytojob.com/apply/abc") == "jazzhr"
    assert provider_kind("https://acme.jobs.personio.de/job/123") == "personio"
    assert provider_kind("https://www.comeet.co/jobs/acme/123") == "comeet"
    assert provider_kind("https://acme.pinpointhq.com/postings/123") == "pinpoint"
    assert provider_kind("https://acme.applicantpro.com/jobs/123") == "applicantpro"


def test_install_is_idempotent() -> None:
    install_coverage_extensions()
    first = career_seed_urls("research.example")
    install_coverage_extensions()
    second = career_seed_urls("research.example")

    assert second == first
