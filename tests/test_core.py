from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gaia import api
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

def test_non_internship_rows_are_not_materialized_as_role_families(tmp_path):
    item = classify(
        Posting(
            company="Anthropic",
            title="Senior Software Engineer",
            apply_url="https://example.com/full-time",
            source="greenhouse:anthropic",
            source_id="full-time",
        )
    )
    db = Database(tmp_path / "gaia.db")
    db.apply_result(CollectorResult("greenhouse:anthropic", [item], True, "board", 1, 1))
    assert db.list_families(target="")["total"] == 0


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


def test_amazon_identity_matches_listing_and_apply_urls():
    listing = application_identity(
        "https://www.amazon.jobs/en/jobs/10418355/2027-software-dev-engineer-intern",
        "schema:www.amazon.jobs:Amazon",
        "listing",
    )
    apply = application_identity(
        "https://www.amazon.jobs/jobs/10418355/apply",
        "registry:test",
        "row",
    )
    assert listing == apply == "amazon:10418355"


def test_database_materializes_family(tmp_path):
    db = Database(tmp_path / "gaia.db")
    one = posting(source_id="1", locations=["PA - Lansdale"])
    two = posting(source_id="2", apply_url="https://example.com/job/2", locations=["OH - Dublin"])
    one.posted_at = datetime(2026, 7, 20, tzinfo=UTC)
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
            posted_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
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
            posted_at=datetime(2026, 7, 19, 12, tzinfo=UTC),
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
    assert coverage["productive_direct_sources"] == 1
    assert coverage["direct_date_coverage_percent"] == 100.0


def test_backend_search_escapes_wildcards_matches_all_terms_and_ranks_company(tmp_path, monkeypatch):
    db = Database(tmp_path / "gaia.db")
    rows = [
        classify(
            Posting(
                company="Google",
                title="Software Engineering Intern, Summer 2027",
                apply_url="https://example.com/google",
                source="direct:test",
                source_id="google",
                locations=["Mountain View, CA"],
            )
        ),
        classify(
            Posting(
                company="Percent 100% Labs",
                title="Data Engineer Intern, Summer 2027",
                apply_url="https://example.com/percent",
                source="direct:test",
                source_id="percent",
            )
        ),
        classify(
            Posting(
                company="Other",
                title="Google Software Intern, Summer 2027",
                apply_url="https://example.com/other",
                source="direct:test",
                source_id="other",
                locations=["Toronto, Canada"],
            )
        ),
    ]
    db.apply_result(CollectorResult("direct:test", rows, True, "board", 3, 3))
    monkeypatch.setattr(api, "db", db)

    common = {
        "category": "",
        "target": "default",
        "track": "tech",
        "trust": "verified",
        "location": "",
        "sort": "newest",
        "page": 1,
        "page_size": 12,
    }
    wildcard = api._list_families(query="100%", **common)
    assert wildcard["total"] == 1
    assert wildcard["items"][0]["company"] == "Percent 100% Labs"

    ranked = api._list_families(query="Google software", **common)
    assert ranked["total"] == 2
    assert [item["company"] for item in ranked["items"]] == ["Google", "Other"]

    california = api._list_families(query="", **{**common, "location": "CA"})
    assert california["total"] == 1
    assert california["items"][0]["company"] == "Google"


    company = api._list_families(query="", **{**common, "company": "Google"})
    assert company["total"] == 1
    assert company["items"][0]["company"] == "Google"
    facet_data = api.facets()
    assert {item["value"] for item in facet_data["companies"]} == {
        "Google",
        "Other",
        "Percent 100% Labs",
    }

def test_database_rejects_rows_without_required_product_identity(tmp_path):
    db = Database(tmp_path / "gaia.db")
    invalid = Posting(
        company="Example",
        title="",
        apply_url="https://example.com/job",
        source="direct:test",
        source_id="invalid",
    )
    db.apply_result(CollectorResult("direct:test", [invalid], True, "board", 1, 1))

    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0


def test_database_rejects_concurrent_sync_runs_and_recovers_after_finish(tmp_path):
    db = Database(tmp_path / "gaia.db")
    first = db.start_run()

    with pytest.raises(RuntimeError, match="already running"):
        db.start_run()

    db.finish_run(first, sources=0, postings=0, failed=0)
    second = db.start_run()
    assert second > first


def test_database_normalizes_locations_at_persistence_boundary(tmp_path):
    db = Database(tmp_path / "gaia.db")
    posting = classify(
        Posting(
            company="Example",
            title="Software Engineering Intern, Summer 2027",
            apply_url="https://example.com/job/1",
            source="direct:test",
            source_id="1",
            locations=["2026-07-24", "(multiple US)"],
        )
    )

    db.apply_result(CollectorResult("direct:test", [posting], True, "board", 1, 1))

    with db.connect() as connection:
        stored = connection.execute("SELECT locations_json FROM postings").fetchone()[0]
    assert stored == '["United States"]'


def test_family_detail_exposes_only_direct_application_links_by_default(tmp_path, monkeypatch):
    db = Database(tmp_path / "gaia.db")
    direct = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://job-boards.greenhouse.io/example/jobs/123",
            source="greenhouse:example",
            source_id="123",
        )
    )
    lead = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://example.com",
            source="registry:test",
            source_id="lead",
            source_mode="external-index",
        )
    )
    db.apply_result(CollectorResult("greenhouse:example", [direct], True, "board", 1, 1))
    db.apply_result(
        CollectorResult("registry:test", [lead], True, "external-index", 1, 1)
    )
    monkeypatch.setattr(api, "db", db)
    family_key = db.list_families(target="")["items"][0]["family_key"]

    verified = api.family(family_key, trust="verified")
    assert [opening["apply_url"] for opening in verified["openings"]] == [
        "https://job-boards.greenhouse.io/example/jobs/123"
    ]
    assert verified["openings"][0]["first_detected_at"]
    assert len(api.family(family_key, trust="all")["openings"]) == 2


def test_source_lifecycle_quarantines_repeated_failures_and_recovers(tmp_path):
    db = Database(tmp_path / "gaia.db")
    failure = CollectorResult(
        "workday:example:external",
        [],
        False,
        "board-search",
        0,
        0,
        error="HTTP 404",
        status="broken",
    )
    for _ in range(3):
        db.record_failure(failure)

    with db.connect() as connection:
        quarantined = connection.execute(
            "SELECT lifecycle, scope, consecutive_failures FROM source_health"
        ).fetchone()
    assert tuple(quarantined) == ("quarantined", "historical", 3)

    recovered = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://example.wd5.myworkdayjobs.com/External/job/Intern_R1",
            source="workday:example:external",
            source_id="R1",
        )
    )
    db.apply_result(
        CollectorResult(
            "workday:example:external", [recovered], True, "board-search", 1, 1
        )
    )
    with db.connect() as connection:
        productive = connection.execute(
            "SELECT lifecycle, scope, consecutive_failures FROM source_health"
        ).fetchone()
    assert tuple(productive) == ("productive", "current", 0)


def test_benchmark_corpus_freezes_deterministic_classification_cases(tmp_path):
    db = Database(tmp_path / "gaia.db")
    rows = [
        classify(
            Posting(
                company=f"Example {index}",
                title="Software Engineer Intern, Summer 2027",
                apply_url=f"https://example.com/job/{index}",
                source="direct:test",
                source_id=str(index),
            )
        )
        for index in range(3)
    ]
    db.apply_result(CollectorResult("direct:test", rows, True, "board", 3, 3))

    assert db.seed_benchmark_corpus(limit=2) == 2
    assert db.seed_benchmark_corpus(limit=3) == 3
    assert db.coverage()["summary"]["benchmark_size"] == 3
