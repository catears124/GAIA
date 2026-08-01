from gaia import career_surface_collector as career
from gaia.domain_graph_coverage_extension import install_domain_graph_coverage_extension


def _install() -> None:
    install_domain_graph_coverage_extension()


def test_www_and_apex_are_same_employer_surface() -> None:
    _install()
    assert career._same_host("https://www.acme.com/careers/jobs/1", "acme.com")
    assert career._same_host("https://acme.com/jobs/1", "www.acme.com")


def test_dedicated_career_subdomains_are_traversable() -> None:
    _install()
    assert career._same_host("https://jobs.acme.com/openings/1", "acme.com")
    assert career._same_host("https://careers.acme.com/jobs/2", "www.acme.com")
    assert career._same_host("https://acme.com/jobs/3", "careers.acme.com")


def test_unrelated_sibling_subdomains_are_not_traversed() -> None:
    _install()
    assert not career._same_host("https://shop.acme.com/products/1", "acme.com")
    assert not career._same_host("https://blog.acme.com/posts/jobs-report", "acme.com")
    assert not career._same_host("https://jobs.other.com/openings/1", "acme.com")


def test_tracking_parameters_do_not_consume_distinct_crawl_slots() -> None:
    _install()
    first = career._normalized_http_url(
        "https://careers.acme.com/jobs/123?gh_jid=123&utm_source=linkedin&gclid=abc"
    )
    second = career._normalized_http_url(
        "https://careers.acme.com/jobs/123?gh_jid=123&utm_campaign=summer"
    )
    assert first == "https://careers.acme.com/jobs/123?gh_jid=123"
    assert second == first


def test_job_semantic_query_parameters_are_preserved() -> None:
    _install()
    normalized = career._normalized_http_url(
        "https://jobs.acme.com/search?page=2&location=US&utm_medium=email"
    )
    assert normalized == "https://jobs.acme.com/search?page=2&location=US"


def test_additional_recruiting_platforms_are_recognized() -> None:
    _install()
    assert career.provider_kind("https://acme.talentbrew.com/jobs/1") == "talentbrew"
    assert career.provider_kind("https://jobs.acme.jobs2web.com/job/1") == "jobs2web"
    assert career.provider_kind("https://acme.radancy.com/jobs/1") == "radancy"
    assert career.provider_kind("https://acme.symplicity.com/students/app/jobs/1") == "symplicity"
    assert career.provider_kind("https://acme.12twenty.com/job/1") == "12twenty"
    assert career.provider_kind("https://acme.simplicant.com/jobs/1") == "simplicant"
    assert career.provider_kind("https://acme.manatal.com/jobs/1") == "manatal"
    assert career.provider_kind("https://acme.comeet.com/jobs/1") == "comeet"


def test_extension_is_idempotent() -> None:
    _install()
    same_host = career._same_host
    normalizer = career._normalized_http_url
    providers = dict(career.PROVIDER_HOST_FRAGMENTS)
    install_domain_graph_coverage_extension()
    assert career._same_host is same_host
    assert career._normalized_http_url is normalizer
    assert career.PROVIDER_HOST_FRAGMENTS == providers
