from __future__ import annotations

from gaia.classify import classify
from gaia.db import Database
from gaia.models import CollectorResult, Posting


def test_same_application_reconciles_before_family_grouping(tmp_path):
    db = Database(tmp_path / "gaia.db")
    direct = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://job-boards.greenhouse.io/example/jobs/123",
            source="greenhouse:example",
            source_id="123",
            locations=["New York, NY"],
        )
    )
    registry = classify(
        Posting(
            company="Example, Inc.",
            title="Summer 2027 Software Engineering Internship - New York",
            apply_url="https://job-boards.greenhouse.io/example/jobs/123?utm_source=registry",
            source="registry:test",
            source_id="row-123",
            source_mode="registry",
            locations=["New York, NY"],
        ),
        source_confirms_2027=True,
    )

    db.apply_result(
        CollectorResult("registry:test", [registry], True, "registry", 1, 1),
        rebuild=False,
    )
    db.apply_result(
        CollectorResult("greenhouse:example", [direct], True, "board", 1, 1),
        rebuild=False,
    )
    db.rebuild_families()

    page = db.list_families()
    assert page["total"] == 1
    family = page["items"][0]
    assert family["company"] == "Example"
    assert family["title"] == "Software Engineer Intern, Summer 2027"
    assert family["opening_count"] == 1
    assert family["direct_openings"] == 1

    db.apply_result(
        CollectorResult("greenhouse:example", [], True, "board", 0, 0),
        rebuild=False,
    )
    db.rebuild_families()
    assert db.list_families()["total"] == 0


def test_complete_greenhouse_board_closes_registry_only_jobs(tmp_path):
    db = Database(tmp_path / "gaia.db")
    registry = classify(
        Posting(
            company="Akuna Capital",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://www.akunacapital.com/careers/position/8018847",
            source="registry:test",
            source_id="8018847",
            source_mode="registry",
        ),
        source_confirms_2027=True,
    )
    db.apply_result(
        CollectorResult("registry:test", [registry], True, "registry", 1, 1),
        rebuild=False,
    )
    db.apply_result(
        CollectorResult(
            "greenhouse:akunacapital",
            [],
            True,
            "board",
            0,
            None,
            status="dormant",
        ),
        rebuild=False,
    )
    db.rebuild_families()
    assert db.list_families()["total"] == 1

    db.apply_result(
        CollectorResult(
            "greenhouse:akunacapital",
            [],
            True,
            "board",
            0,
            0,
            status="empty",
        ),
        rebuild=False,
    )
    db.rebuild_families()
    assert db.list_families()["total"] == 0


def test_coverage_matches_recovered_role_when_employer_changes_url(tmp_path):
    db = Database(tmp_path / "gaia.db")
    registry = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://example.com/careers/123",
            source="registry:test",
            source_id="123",
            source_mode="registry",
        ),
        source_confirms_2027=True,
    )
    verified = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://example.com/careers/software-engineer-intern-summer-2027",
            source="schema:example.com:Example",
            source_id="verified",
            source_mode="verification",
        )
    )
    db.apply_result(
        CollectorResult("registry:test", [registry], True, "registry", 1, 1),
        rebuild=False,
    )
    db.apply_result(
        CollectorResult(
            "schema:example.com:Example",
            [verified],
            True,
            "verification",
            1,
            1,
            status="verified",
        ),
        rebuild=False,
    )

    coverage = db.coverage()["summary"]
    assert coverage["registry_floor"] == 1
    assert coverage["independent_matches"] == 1
    assert coverage["registry_recall_percent"] == 100.0
