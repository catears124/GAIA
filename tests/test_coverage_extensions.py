from __future__ import annotations

from gaia import career_surface_collector as career
from gaia.career_surface_collector import career_seed_urls, provider_kind
from gaia.coverage_extensions import install_coverage_extensions


def test_install_expands_early_career_search_and_feed_entry_points() -> None:
    install_coverage_extensions()

    seeds = career_seed_urls("research.example")

    assert "https://research.example/internships" in seeds
    assert "https://research.example/early-careers" in seeds
    assert "https://research.example/university-recruiting" in seeds
    assert "https://research.example/jobs/search" in seeds
    assert "https://research.example/jobs/feed" in seeds
    assert "https://research.example/careers.rss" in seeds


def test_install_recognizes_additional_shared_hosted_ats_products() -> None:
    install_coverage_extensions()

    assert provider_kind("https://acme.breezy.hr/p/abc-software-intern") == "breezy"
    assert provider_kind("https://acme.applytojob.com/apply/abc") == "jazzhr"
    assert provider_kind("https://acme.jobs.personio.de/job/123") == "personio"
    assert provider_kind("https://www.comeet.co/jobs/acme/123") == "comeet"
    assert provider_kind("https://acme.pinpointhq.com/postings/123") == "pinpoint"
    assert provider_kind("https://acme.applicantpro.com/jobs/123") == "applicantpro"
    assert provider_kind("https://acme.jobsoid.com/job/123") == "jobsoid"
    assert provider_kind("https://acme.join.com/jobs/123") == "join"
    assert provider_kind("https://acme.zohorecruit.com/jobs/123") == "zoho-recruit"
    assert provider_kind("https://jobs.eightfold.ai/careers/job/123") == "eightfold"
    assert provider_kind("https://acme.phenompeople.com/us/en/job/123") == "phenom"
    assert provider_kind("https://acme.avature.net/careers/JobDetail/123") == "avature"
    assert provider_kind("https://jobs.dayforcehcm.com/en-US/acme/jobs/123") == "dayforce"
    assert provider_kind("https://www.paycomonline.net/v4/ats/web.php/jobs/123") == "paycom"


def test_install_recovers_provider_links_from_embedded_elements() -> None:
    install_coverage_extensions()

    links = dict(
        career._links(
            """
            <iframe src="https://acme.breezy.hr/"></iframe>
            <form action="https://acme.applytojob.com/apply/123"></form>
            <script src="https://acme.jobs.personio.de/widget/job-list.js"></script>
            <div data-careers-url="https://acme.join.com/jobs"></div>
            """,
            "https://www.acme.example/careers",
        )
    )

    assert "https://acme.breezy.hr/" in links
    assert "https://acme.applytojob.com/apply/123" in links
    assert "https://acme.jobs.personio.de/widget/job-list.js" in links
    assert "https://acme.join.com/jobs" in links


def test_install_recovers_jobposting_json_ld_and_career_redirects() -> None:
    install_coverage_extensions()

    links = dict(
        career._links(
            """
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@type": "JobPosting",
              "title": "Software Engineering Intern",
              "url": "https://jobs.eightfold.ai/careers/job/123",
              "applicationUrl": "/careers/software-intern"
            }
            </script>
            <meta http-equiv="refresh" content="0; URL=https://acme.avature.net/careers">
            """,
            "https://www.acme.example/jobs",
        )
    )

    assert links["https://jobs.eightfold.ai/careers/job/123"] == "json-ld:JobPosting"
    assert links["https://www.acme.example/careers/software-intern"] == "json-ld:JobPosting"
    assert links["https://acme.avature.net/careers"] == "meta:refresh"


def test_structured_recovery_ignores_non_job_schema_urls() -> None:
    install_coverage_extensions()

    links = dict(
        career._links(
            """
            <script type="application/ld+json">
            {"@type":"Organization","url":"https://www.acme.example/about"}
            </script>
            <script type="application/ld+json">not valid json</script>
            """,
            "https://www.acme.example/careers",
        )
    )

    assert "https://www.acme.example/about" not in links


def test_embedded_link_recovery_ignores_unrelated_assets_and_forms() -> None:
    install_coverage_extensions()

    links = dict(
        career._links(
            """
            <script src="https://cdn.example/analytics.js"></script>
            <iframe src="https://video.example/embed/123"></iframe>
            <form action="/newsletter/subscribe"></form>
            <div data-url="/pricing"></div>
            """,
            "https://www.acme.example/careers",
        )
    )

    assert "https://cdn.example/analytics.js" not in links
    assert "https://video.example/embed/123" not in links
    assert "https://www.acme.example/newsletter/subscribe" not in links
    assert "https://www.acme.example/pricing" not in links


def test_install_is_idempotent() -> None:
    install_coverage_extensions()
    first = career_seed_urls("research.example")
    first_links = career._links
    install_coverage_extensions()
    second = career_seed_urls("research.example")

    assert second == first
    assert career._links is first_links
