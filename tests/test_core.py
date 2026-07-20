from __future__ import annotations

from datetime import datetime, timezone

from gaia.classify import classify
from gaia.db import Database
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
