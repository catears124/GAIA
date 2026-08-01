from __future__ import annotations

from gaia import career_surface_collector as career
from gaia.career_surface_collector import career_seed_urls, provider_kind
from gaia.coverage_extensions import install_coverage_extensions
from gaia.runtime_coverage_extensions import install_runtime_coverage_extensions


def _install() -> None:
    install_coverage_extensions()
    install_runtime_coverage_extensions()


def test_runtime_extensions_add_job_sitemap_entry_points() -> None:
    _install()

    seeds = career_seed_urls("research.example")

    assert "https://research.example/job-sitemap.xml" in seeds
    assert "https://research.example/careers-sitemap.xml" in seeds
    assert "https://research.example/sitemap_jobs.xml" in seeds


def test_runtime_extensions_recognize_additional_hosted_ats_products() -> None:
    _install()

    assert provider_kind("https://acme.bamboohr.com/careers/123") == "bamboohr"
    assert provider_kind("https://acme.hiringthing.com/job/123") == "hiringthing"
    assert provider_kind("https://acme.applicantstack.com/x/detail/123") == "applicantstack"
    assert provider_kind("https://acme.exacthire.com/All_Applicants/123") == "exacthire"
    assert provider_kind("https://careers.hireology.com/acme/123") == "hireology"
    assert provider_kind("https://acme.newtonsoftware.com/careers/job/123") == "newton"
    assert provider_kind("https://recruiting.paylocity.com/recruiting/jobs/Details/123") == "paylocity"
    assert provider_kind("https://acme.hirecentric.com/jobs/123") == "hirecentric"
    assert provider_kind("https://acme.hrmdirect.com/employment/job-opening.php?req=123") == "hrmdirect"


def test_runtime_extensions_recover_inline_script_provider_configs() -> None:
    _install()

    links = dict(
        career._links(
            """
            <script>
              window.jobsConfig = {
                board: "https:\/\/acme.bamboohr.com\/careers",
                apply: "//acme.hiringthing.com/job/123",
                relative: "/careers/software-engineering-intern"
              };
            </script>
            """,
            "https://www.acme.example/careers",
        )
    )

    assert links["https://acme.bamboohr.com/careers"] == "script:inline-config"
    assert links["https://acme.hiringthing.com/job/123"] == "script:inline-config"
    assert (
        links["https://www.acme.example/careers/software-engineering-intern"]
        == "script:inline-config"
    )


def test_runtime_extensions_ignore_inline_analytics_and_noncareer_urls() -> None:
    _install()

    links = dict(
        career._links(
            """
            <script>
              const analytics = "https://metrics.example/collect";
              const pricing = "/pricing";
              const video = "https://video.example/watch/123";
            </script>
            """,
            "https://www.acme.example/careers",
        )
    )

    assert "https://metrics.example/collect" not in links
    assert "https://www.acme.example/pricing" not in links
    assert "https://video.example/watch/123" not in links


def test_runtime_extensions_are_idempotent() -> None:
    _install()
    first_links = career._links
    first_seeds = career_seed_urls("research.example")

    install_runtime_coverage_extensions()

    assert career._links is first_links
    assert career_seed_urls("research.example") == first_seeds
