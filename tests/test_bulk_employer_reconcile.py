from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

from gaia.db import Database
from gaia.employer_census import (
    ensure_ecosystem_schema,
    merge_observations_into_universe,
)
from gaia.universe import _employer_key, ensure_universe_schema


def test_ecosystem_observations_merge_in_bulk(tmp_path) -> None:
    database = Database(tmp_path / "bulk-employer-reconcile.db")
    ensure_ecosystem_schema(database)
    ensure_universe_schema(database)
    now = datetime.now(UTC)
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO employer_observations(
                observation_key, canonical_name, aliases, evidence_type, source,
                profile_url, official_url, internship_signal, technical_signal,
                first_seen_at, last_seen_at, metadata
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                (
                    "obs-1",
                    "Example Labs",
                    ["Example Labs"],
                    "startup-ecosystem",
                    "yc:test",
                    "https://example.test/profile",
                    None,
                    0.4,
                    0.9,
                    now - timedelta(days=2),
                    now - timedelta(days=1),
                    Jsonb({"batch": "W26"}),
                ),
                (
                    "obs-2",
                    "Example Labs",
                    ["Example"],
                    "employer-page",
                    "directory:test",
                    "https://example.test/directory",
                    "https://example.test",
                    0.8,
                    0.7,
                    now - timedelta(days=3),
                    now,
                    Jsonb({"verified": True}),
                ),
            ],
        )

    result = merge_observations_into_universe(database)

    with database.connect() as connection:
        employer = connection.execute(
            "SELECT * FROM employer_universe WHERE employer_key=%s",
            (_employer_key("Example Labs"),),
        ).fetchone()
        evidence = connection.execute(
            "SELECT COUNT(*) AS count FROM employer_evidence WHERE employer_key=%s",
            (_employer_key("Example Labs"),),
        ).fetchone()

    assert result == {"observations": 2, "merged": 1, "inserted": 1}
    assert employer is not None
    assert employer["resolution_status"] == "located"
    assert employer["evidence_count"] == 2
    assert set(employer["aliases"]) == {"Example", "Example Labs"}
    assert employer["internship_probability"] == 0.8
    assert employer["technical_probability"] == 0.9
    assert evidence["count"] == 2


def test_reconcile_has_no_per_observation_database_loop() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "gaia" / "employer_census.py"
    ).read_text(encoding="utf-8")
    function = source.split(
        "def merge_observations_into_universe(database: Database)", 1
    )[1]

    assert 'SELECT * FROM employer_universe WHERE employer_key=%s' not in function
    assert function.count("connection.executemany(") == 2


def test_superseded_reconcile_status_is_not_failure() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "reconcile.yml"
    ).read_text(encoding="utf-8")

    assert "Reconciliation superseded by a newer pulse" in workflow
    assert "state=pending" in workflow
