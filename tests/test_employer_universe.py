from __future__ import annotations

from datetime import UTC, datetime

from gaia.db import Database
from gaia.employer_census import (
    _upsert_observations,
    _yc_observations,
    ensure_ecosystem_schema,
    merge_observations_into_universe,
)
from gaia.health import production_report
from gaia.models import CollectorResult, Posting
from gaia.product_api import _live_order_clause
from gaia.universe import (
    ensure_universe_schema,
    rebuild_employer_universe,
    universe_summary,
)


def _result(posting: Posting, *, mode: str, complete: bool = True) -> CollectorResult:
    return CollectorResult(
        source=posting.source,
        postings=[posting],
        complete=complete,
        mode=mode,
        rows_scanned=1,
        expected_rows=1 if complete else None,
        status="ok",
        scope="current",
    )


def test_yc_directory_creates_employer_observations_not_jobs() -> None:
    body = """
    <html><body>
      <a href="/companies/quiet-robotics">Quiet Robotics W2025 Active • 7 employees</a>
      <a href="/companies/quiet-robotics/jobs">Jobs</a>
      <a href="/companies">Startup Directory</a>
    </body></html>
    """

    rows = _yc_observations(
        body,
        url="https://www.ycombinator.com/companies/industry/hard-tech",
        source="yc-hard-tech",
        sectors=["hard-tech", "robotics"],
    )

    assert rows == [
        {
            "name": "Quiet Robotics",
            "profile_url": "https://www.ycombinator.com/companies/quiet-robotics",
            "location": "",
            "sectors": ["hard-tech", "robotics"],
            "metadata": {
                "directory_url": "https://www.ycombinator.com/companies/industry/hard-tech",
                "slug": "quiet-robotics",
            },
        }
    ]


def test_historical_technical_employer_is_a_blind_spot_until_enumerated(tmp_path) -> None:
    database = Database(tmp_path / "universe.db")
    ensure_universe_schema(database)
    historical = Posting(
        company="Quiet Robotics",
        title="Robotics Software Intern",
        apply_url="https://quiet.example/careers/robotics-intern-2025",
        source="universe-seed:2025",
        source_id="quiet-2025",
        source_mode="universe-seed",
        category="software",
        year=2025,
        target_match="year_confirmed",
    )
    database.apply_result(_result(historical, mode="registry"), rebuild=False)

    rebuild_employer_universe(database)
    report = universe_summary(database)
    quiet = next(item for item in report["frontier"] if item["canonical_name"] == "Quiet Robotics")

    assert quiet["resolution_status"] == "historical"
    assert quiet["blind_spot"] is True
    assert quiet["frontier_score"] > 0


def test_validated_direct_source_enumerates_employer(tmp_path) -> None:
    database = Database(tmp_path / "enumerated.db")
    ensure_universe_schema(database)
    source = "greenhouse:quiet-robotics"
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO source_catalog(source, kind, scope, spec, validated, origin)
            VALUES (%s,'greenhouse','current','{}'::jsonb,TRUE,'test')
            """,
            (source,),
        )
    direct = Posting(
        company="Quiet Robotics",
        title="Robotics Software Intern",
        apply_url="https://job-boards.greenhouse.io/quiet-robotics/jobs/123",
        source=source,
        source_id="123",
        source_mode="direct",
        category="software",
        year=2027,
        target_match="exact",
    )
    database.apply_result(_result(direct, mode="board"), rebuild=False)

    rebuild_employer_universe(database)
    summary = universe_summary(database)["summary"]

    assert summary["known_employers"] == 1
    assert summary["enumerated_employers"] == 1
    assert summary["unresolved_employers"] == 0


def test_ecosystem_only_employer_enters_unresolved_frontier(tmp_path) -> None:
    database = Database(tmp_path / "ecosystem.db")
    ensure_universe_schema(database)
    ensure_ecosystem_schema(database)
    _upsert_observations(
        database,
        source="yc:hard-tech",
        evidence_type="startup-ecosystem",
        internship_signal=0.3,
        technical_signal=0.94,
        observations=[
            {
                "name": "Under The Radar Systems",
                "profile_url": "https://www.ycombinator.com/companies/under-the-radar",
                "location": "",
                "sectors": ["hard-tech"],
                "metadata": {},
            }
        ],
    )

    rebuild_employer_universe(database)
    merge_observations_into_universe(database)
    report = universe_summary(database)
    employer = next(
        item
        for item in report["frontier"]
        if item["canonical_name"] == "Under The Radar Systems"
    )

    assert report["summary"]["known_employers"] == 1
    assert report["summary"]["enumerated_employers"] == 0
    assert employer["blind_spot"] is True
    assert employer["resolution_status"] == "candidate"


def test_recent_order_prefers_employer_date_then_precision_then_detection() -> None:
    order = _live_order_clause("newest")

    assert order.startswith("(latest_posted_at IS NOT NULL) DESC")
    assert "latest_posted_at DESC NULLS LAST" in order
    assert "CASE posted_precision" in order
    assert order.index("latest_posted_at DESC") < order.index("CASE posted_precision")
    assert order.index("CASE posted_precision") < order.index("first_detected_at DESC")


def test_universe_timestamps_are_timezone_aware(tmp_path) -> None:
    database = Database(tmp_path / "timestamps.db")
    ensure_universe_schema(database)
    now = datetime.now(UTC)
    posting = Posting(
        company="Timestamp Labs",
        title="Data Intern",
        apply_url="https://example.com/jobs/data-intern",
        source="registry:test",
        source_id="data",
        source_mode="registry",
        category="data",
        year=2027,
        target_match="exact",
        observed_at=now,
    )
    database.apply_result(_result(posting, mode="registry"), rebuild=False)

    rebuild_employer_universe(database)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT first_seen_at, last_seen_at FROM employer_universe"
        ).fetchone()

    assert row["first_seen_at"].tzinfo is not None
    assert row["last_seen_at"].tzinfo is not None


def test_production_checker_can_require_employer_universe(tmp_path) -> None:
    database = Database(tmp_path / "missing-universe.db")

    report = production_report(
        database,
        min_sources=1,
        min_active_listings=1,
        require_universe=True,
    )

    assert report["ok"] is False
    assert "employer universe contains no employers" in report["errors"]
