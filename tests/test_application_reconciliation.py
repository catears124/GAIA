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

    assert direct.family_key if hasattr(direct, "family_key") else True
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
