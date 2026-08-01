from __future__ import annotations

from gaia.activity_metrics import stall_assessment
from gaia import career_surface_collector as career
from gaia.provider_expansion import install_provider_expansion


def test_stall_assessment_flags_visible_inventory_and_candidate_backlog() -> None:
    result = stall_assessment(
        {"newest_visible_activity_age_hours": 72.0},
        {"oldest_due_age_minutes": 240.0},
    )
    assert result == {
        "healthy": False,
        "failures": [
            "visible_listing_activity_stalled",
            "source_candidate_backlog_stalled",
        ],
    }


def test_stall_assessment_accepts_advancing_product() -> None:
    result = stall_assessment(
        {"newest_visible_activity_age_hours": 3.0},
        {"oldest_due_age_minutes": 20.0},
    )
    assert result == {"healthy": True, "failures": []}


def test_additional_hosted_providers_are_tenant_scoped_candidates() -> None:
    install_provider_expansion()
    expected = {
        "https://acme.clearcompany.com/careers/jobs/123": "clearcompany",
        "https://jobs.hirebridge.com/v3/Jobs/JobDetails.aspx?jid=123": "hirebridge",
        "https://acme.silkroad.com/epostings/index.cfm?fuseaction=app.jobinfo&jobid=123": "silkroad",
        "https://jobs.peoplefluent.com/acme/job/123": "peoplefluent",
        "https://acme.talentreef.com/career/job/123": "talentreef",
        "https://www.governmentjobs.com/careers/acme/jobs/123": "neogov",
        "https://acme.careerplug.com/jobs/123": "careerplug",
    }
    for url, kind in expected.items():
        assert career.provider_kind(url) == kind
