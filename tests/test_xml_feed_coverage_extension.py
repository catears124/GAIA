from __future__ import annotations

from gaia import career_surface_collector as career
from gaia.json_feed_coverage_extension import install_json_feed_coverage_extension
from gaia.xml_feed_coverage_extension import install_xml_feed_coverage_extension


def _install() -> None:
    install_json_feed_coverage_extension()
    install_xml_feed_coverage_extension()


def test_custom_xml_job_feed_discovers_detail_and_apply_urls() -> None:
    _install()
    body = """
    <jobs>
      <job>
        <job-title>Software Engineering Intern</job-title>
        <job-id>123</job-id>
        <job-url>/careers/positions/123</job-url>
        <apply-url>https://jobs.applytojob.com/acme/123</apply-url>
      </job>
    </jobs>
    """

    links, is_feed = career._document_links(body, "https://acme.example/jobs.xml")
    urls = {url for url, _label in links}

    assert is_feed is True
    assert "https://acme.example/careers/positions/123" in urls
    assert "https://jobs.applytojob.com/acme/123" in urls


def test_rss_item_with_job_shaped_fields_is_a_job_feed() -> None:
    _install()
    body = """
    <rss><channel><item>
      <title>Machine Learning Intern</title>
      <guid>ml-intern-42</guid>
      <link>https://acme.example/openings/ml-intern-42</link>
    </item></channel></rss>
    """

    links, is_feed = career._document_links(body, "https://acme.example/careers/feed")

    assert is_feed is True
    assert ("https://acme.example/openings/ml-intern-42", "link") in links


def test_unrelated_xml_catalog_is_not_promoted_to_job_feed() -> None:
    _install()
    body = """
    <catalog><item><title>Office chair</title><link>https://shop.example/products/chair</link></item></catalog>
    """

    links, is_feed = career._document_links(body, "https://acme.example/catalog.xml")

    assert is_feed is False
    assert "https://shop.example/products/chair" not in {url for url, _label in links}


def test_json_and_xml_parsers_compose_without_clobbering() -> None:
    _install()

    json_links, json_feed = career._document_links(
        '{"jobs":[{"id":1,"title":"Data Intern","url":"/jobs/1"}]}',
        "https://acme.example/api/jobs",
    )
    xml_links, xml_feed = career._document_links(
        "<jobs><job><title>Quant Intern</title><id>2</id><url>/jobs/2</url></job></jobs>",
        "https://acme.example/jobs.xml",
    )

    assert json_feed is True
    assert ("https://acme.example/jobs/1", "url") in json_links
    assert xml_feed is True
    assert ("https://acme.example/jobs/2", "url") in xml_links


def test_additional_recruiting_platforms_are_recognized() -> None:
    _install()

    assert career.provider_kind("https://jobs.applytojob.com/acme/123") == "apply-to-job"
    assert career.provider_kind("https://acme.hirehive.com/job/123") == "hirehive"
    assert career.provider_kind("https://acme.careers-page.com/jobs/123") == "careers-page"
    assert career.provider_kind("https://jobs.jobtrain.co.uk/acme/job/123") == "jobtrain"
    assert career.provider_kind("https://acme.eploy.net/vacancies/123") == "eploy"
    assert career.provider_kind("https://apply.vacancy-filler.co.uk/acme/123") == "vacancy-filler"


def test_xml_extension_is_idempotent() -> None:
    _install()
    parser = career._document_links
    providers = dict(career.PROVIDER_HOST_FRAGMENTS)

    install_xml_feed_coverage_extension()

    assert career._document_links is parser
    assert career.PROVIDER_HOST_FRAGMENTS == providers
