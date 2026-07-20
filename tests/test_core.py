from __future__ import annotations

from datetime import datetime, timezone

from gaia.classify import classify
from gaia.db import Database, application_identity
from gaia.grouping import family_key, normalize_title
from gaia.models import CollectorResult, Posting


def posting(**overrides):
    values = {
        "company": "CVS Health",
        "title": "Pharmacy Intern",
        "apply_url": "https://example.com/job/1",
        "source": "workday:cvs:jobs",
        "source_id": "1",
        "locations": ["PA - Lansdale"],
        "employment_type": "Part time",
    }
    values.update(overrides)
    return classify(Posting(**values), source_confirms_2027=True)


def test_role_family_groups_location_requisitions():
    left = posting(source_id="1", locations=["PA - Lansdale"])
    right = posting(source_id="2", apply_url="https://example.com/job/2", locations=["OH - Dublin"])
    assert left.posting_key != right.posting_key
    assert family_key(left) == family_key(right)


def test_distinct_program_not_overgrouped():
    normal = posting()
    international = posting(
        source_id="2",
        title="Foreign Pharmacy Grad - International Pharmacy Intern",
        apply_url="https://example.com/job/2",
    )
    assert family_key(normal) != family_key(international)


def test_seasonless_registry_role_is_not_promoted_to_summer_2027():
    assert posting().target_match == "unknown"


def test_fellowship_does_not_enter_internship_feed():
    item = classify(
        Posting(
            company="Anthropic",
            title="Anthropic Fellows Program",
            apply_url="https://example.com/fellows",
            source="greenhouse:anthropic",
            source_id="fellows",
            description="A four month full-time research program for technical talent.",
        ),
        source_confirms_2027=True,
    )
    assert item.target_match == "not_internship"


def test_explicit_google_summer_2027_is_exact():
    item = classify(
        Posting(
            company="Google",
            title="Software Engineering Intern, BS, Summer 2027",
            apply_url="https://google.com/job/1",
            source="google-careers",
            source_id="1",
        )
    )
    assert item.target_match == "exact"
    assert item.category == "software"


def test_title_normalization_is_conservative():
    assert normalize_title("Software Engineer Intern — Austin, TX") == "software engineer intern"
    assert normalize_title("Software Engineer Intern - Machine Learning") != normalize_title(
        "Software Engineer Intern - Security"
    )


def test_workday_identity_matches_across_direct_and_registry_sources():
    url = "https://example.wd5.myworkdayjobs.com/External/job/Austin/Intern_R123"
    direct = application_identity(url, "workday:example:External", "R123")
    registry = application_identity(url, "registry:test", "row-1")
    assert direct == registry


def test_database_materializes_family(tmp_path):
    db = Database(tmp_path / "gaia.db")
    one = posting(source_id="1", locations=["PA - Lansdale"])
    two = posting(source_id="2", apply_url="https://example.com/job/2", locations=["OH - Dublin"])
    one.posted_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    one.posted_precision = "day"
    result = CollectorResult("workday:cvs:jobs", [one, two], True, "board", 2, 2)
    db.apply_result(result)
    page = db.list_families(target="")
    assert page["total"] == 1
    family = page["items"][0]
    assert family["opening_count"] == 2
    assert family["location_count"] == 2
    assert family["posted_precision"] == "day"


def test_direct_and_registry_copies_are_one_opening_and_registry_date_is_not_employer_date(
    tmp_path,
):
    db = Database(tmp_path / "gaia.db")
    direct = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://job-boards.greenhouse.io/example/jobs/123",
            source="greenhouse:example",
            source_id="123",
            locations=["New York, NY"],
            posted_at=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
            posted_precision="timestamp",
            posted_confidence="official",
        )
    )
    registry = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://job-boards.greenhouse.io/example/jobs/123?utm_source=tracker",
            source="registry:test",
            source_id="row-1",
            source_mode="registry",
            locations=["New York, NY"],
            posted_at=datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
            posted_precision="timestamp",
            posted_confidence="registry-reported",
        ),
        source_confirms_2027=True,
    )
    db.apply_result(CollectorResult("registry:test", [registry], True, "registry", 1, 1))
    db.apply_result(CollectorResult("greenhouse:example", [direct], True, "board", 1, 1))

    family = db.list_families()["items"][0]
    assert family["opening_count"] == 1
    assert family["direct_openings"] == 1
    assert family["backstop_openings"] == 0
    assert family["latest_posted_at"] == "2026-07-20T12:00:00+00:00"

    coverage = db.coverage()["summary"]
    assert coverage["registry_floor"] == 1
    assert coverage["direct_matches"] == 1
    assert coverage["registry_only"] == 0
    assert coverage["registry_recall_percent"] == 100.0
