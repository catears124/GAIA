from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gaia.db import Database

REQUIRED_COLUMNS = {
    "posting_key",
    "family_key",
    "company",
    "title",
    "locations",
    "apply_url",
    "canonical_apply_url",
    "source",
    "source_id",
    "source_mode",
    "posted_at",
    "posted_precision",
    "first_seen_at",
    "last_seen_at",
    "category",
    "season",
    "year",
    "target_match",
}


def test_family_rebuild_projects_only_required_columns() -> None:
    source = (Path(__file__).parents[1] / "src" / "gaia" / "db_write.py").read_text(
        encoding="utf-8"
    )
    rebuild = source[source.index("    def rebuild_families") :]
    projection = rebuild.split("FROM postings", 1)[0]

    assert "SELECT *" not in projection
    assert "description" not in projection
    for column in REQUIRED_COLUMNS:
        assert column in projection


def test_family_rebuild_does_not_need_large_descriptions(tmp_path) -> None:
    database = Database(tmp_path / "family-projection.db")
    now = datetime.now(UTC)
    huge_description = "technical internship details " * 50_000

    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO postings(
                posting_key, family_key, company, title, normalized_title, locations,
                apply_url, canonical_apply_url, source, source_id, source_mode,
                description, employment_type, posted_at, posted_precision,
                posted_confidence, first_seen_at, last_seen_at, active, category,
                season, year, target_match
            ) VALUES (
                'posting-1', 'family-1', 'Small Technical Company',
                'Software Engineer Intern', 'software engineer intern', ARRAY['Remote'],
                'https://boards.greenhouse.io/example/jobs/123',
                'https://boards.greenhouse.io/example/jobs/123',
                'greenhouse:example', '123', 'direct', %s, 'Intern', %s,
                'timestamp', 'high', %s, %s, TRUE, 'software', 'summer', 2027, 'exact'
            )
            """,
            (huge_description, now, now, now),
        )

    database.rebuild_families()

    with database.connect() as connection:
        row = connection.execute(
            "SELECT company, title, opening_count FROM families WHERE family_key='family-1'"
        ).fetchone()

    assert row is not None
    assert row["company"] == "Small Technical Company"
    assert row["title"] == "Software Engineer Intern"
    assert row["opening_count"] == 1
