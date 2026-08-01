from __future__ import annotations

import json

from gaia import career_surface_collector as career
from gaia.json_feed_coverage_extension import install_json_feed_coverage_extension


def _install() -> None:
    install_json_feed_coverage_extension()


def test_greenhouse_style_json_jobs_are_discovered() -> None:
    _install()
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 123,
                    "title": "Software Engineering Intern",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
                }
            ]
        }
    )

    links, is_feed = career._document_links(body, "https://acme.example/jobs.json")

    assert is_feed is True
    assert ("https://boards.greenhouse.io/acme/jobs/123", "absolute_url") in links


def test_ashby_style_apply_url_and_relative_details_are_discovered() -> None:
    _install()
    body = json.dumps(
        {
            "jobPostings": [
                {
                    "id": "intern-42",
                    "title": "Machine Learning Intern",
                    "jobUrl": "/careers/jobs/intern-42",
                    "applyUrl": "https://jobs.ashbyhq.com/acme/intern-42/application",
                }
            ]
        }
    )

    links, is_feed = career._document_links(body, "https://acme.example/api/jobs")
    urls = {url for url, _label in links}

    assert is_feed is True
    assert "https://acme.example/careers/jobs/intern-42" in urls
    assert "https://jobs.ashbyhq.com/acme/intern-42/application" in urls


def test_nested_pagination_link_is_followed() -> None:
    _install()
    body = json.dumps(
        {
            "data": {"items": []},
            "links": {"next_page_url": "/api/jobs?page=2"},
        }
    )

    links, is_feed = career._document_links(body, "https://acme.example/api/jobs?page=1")

    assert is_feed is True
    assert ("https://acme.example/api/jobs?page=2", "next_page_url") in links


def test_unrelated_json_urls_are_rejected() -> None:
    _install()
    body = json.dumps(
        {
            "analytics": {"url": "https://tracking.example/collect"},
            "logo": {"url": "https://cdn.example/logo.png"},
            "items": [{"name": "Office chair", "url": "https://shop.example/products/chair"}],
        }
    )

    links, is_feed = career._document_links(body, "https://acme.example/api/catalog")

    assert is_feed is False
    assert not {url for url, _label in links} & {
        "https://tracking.example/collect",
        "https://cdn.example/logo.png",
        "https://shop.example/products/chair",
    }


def test_additional_recruiting_platforms_are_recognized() -> None:
    _install()

    assert career.provider_kind("https://app.crelate.com/portal/acme/job/123") == "crelate"
    assert career.provider_kind("https://jobs.recruitcrm.io/acme/123") == "recruit-crm"
    assert career.provider_kind("https://app.loxo.co/job/123") == "loxo"
    assert career.provider_kind("https://jobs.vincere.io/acme/123") == "vincere"
    assert career.provider_kind("https://acme.bullhornstaffing.com/job/123") == "bullhorn"
    assert career.provider_kind("https://jobs.ceipal.com/acme/123") == "ceipal"
    assert career.provider_kind("https://jobs.trackerrms.com/acme/123") == "tracker-rms"
    assert career.provider_kind("https://jobs.pcrecruiter.net/pcrbin/jobboard.aspx?jobid=123") == "pcrecruiter"


def test_json_extension_is_idempotent() -> None:
    _install()
    first_parser = career._document_links
    first_providers = dict(career.PROVIDER_HOST_FRAGMENTS)

    install_json_feed_coverage_extension()

    assert career._document_links is first_parser
    assert career.PROVIDER_HOST_FRAGMENTS == first_providers
