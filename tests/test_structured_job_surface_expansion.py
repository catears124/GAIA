from __future__ import annotations

from gaia import career_surface_collector as career
from gaia.career_surface_collector import provider_kind
from gaia.coverage_extensions import install_coverage_extensions


def test_nested_jobposting_actions_keep_application_context() -> None:
    install_coverage_extensions()
    links = dict(
        career._links(
            """
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@graph": [{
                "@type": "JobPosting",
                "title": "Machine Learning Intern",
                "potentialAction": {
                  "@type": "ApplyAction",
                  "target": "/apply/req-789"
                }
              }]
            }
            </script>
            <a itemprop="applicationUrl" href="https://acme.taleo.net/careersection/jobdetail.ftl?job=456"></a>
            """,
            "https://www.acme.example/jobs/ml-intern-789",
        )
    )

    assert links["https://www.acme.example/apply/req-789"] == "json-ld:JobPosting"
    assert links["https://acme.taleo.net/careersection/jobdetail.ftl?job=456"] == "microdata:applicationurl"


def test_enterprise_and_midmarket_ats_hosts_are_tenant_scoped_sources() -> None:
    install_coverage_extensions()

    assert provider_kind("https://acme.taleo.net/careersection/jobdetail.ftl?job=123") == "taleo"
    assert provider_kind("https://acme.brassring.com/TGnewUI/Search/home/HomeWithPreLoad") == "brassring"
    assert provider_kind("https://acme.csod.com/ux/ats/careersite/4/home") == "cornerstone"
    assert provider_kind("https://workforcenow.adp.com/mascsr/default/mdf/recruitment") == "adp-workforce-now"
    assert provider_kind("https://jobs.jobadder.com/acme/123") == "jobadder"
    assert provider_kind("https://careers.tribepad.com/members/modules/job/detail.php?record=123") == "tribepad"
    assert provider_kind("https://jobs.fountain.com/acme/opening/123") == "fountain"
    assert provider_kind("https://acme.myworkday.com/jobs/job/123") == "workday"
