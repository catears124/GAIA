from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gaia.db import Database
from gaia.health import (
    listing_freshness_state,
    production_report,
    source_growth_state,
)


def _add_target(database: Database, source: str, *, complete_at: datetime) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO source_catalog(source, kind, scope, spec, validated, origin)
            VALUES (%s,'greenhouse','current','{}'::jsonb,TRUE,'test')
            """,
            (source,),
        )
        connection.execute(
            """
            INSERT INTO crawl_targets(
                source, enabled, scheduled, priority, interval_seconds,
                next_run_at, last_complete_at, last_finished_at, last_status
            ) VALUES (%s,TRUE,TRUE,10,900,now(),%s,%s,'ok')
            """,
            (source, complete_at, complete_at),
        )


def _add_family(database: Database, key: str, *, activity_at: datetime) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO families(
                family_key, company, title, category, season, year, target_match,
                opening_count, location_count, locations, openings, posted_precision,
                latest_posted_at, first_detected_at, last_verified_at,
                direct_openings, backstop_openings
            ) VALUES (
                %s, %s, 'Software Engineer Intern', 'software', 'summer', 2027,
                'exact', 1, 1, ARRAY['Remote'], '[]'::jsonb, 'timestamp',
                %s, %s, %s, 1, 0
            )
            """,
            (key, key, activity_at, activity_at, activity_at),
        )


def test_source_growth_reports_unique_additions_and_probe_backlog(tmp_path) -> None:
    database = Database(tmp_path / "source-growth.db")
    now = datetime.now(UTC)
    _add_target(database, "greenhouse:known", complete_at=now)
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE source_catalog
            SET first_discovered_at=now() - interval '2 days',
                last_discovered_at=now() - interval '2 hours'
            WHERE source='greenhouse:known'
            """
        )
        connection.execute(
            """
            INSERT INTO source_candidates(
                source, kind, scope, spec, origin,
                first_seen_at, last_seen_at, next_probe_at
            ) VALUES (
                'ashby:new-board', 'ashby', 'current', '{}', 'recursive-source:test',
                now() - interval '30 minutes', now() - interval '5 minutes',
                now() - interval '20 minutes'
            )
            """
        )

    state = source_growth_state(database, now=now)

    assert state["catalog_total"] == 1
    assert state["validated_total"] == 1
    assert state["candidate_total"] == 1
    assert state["due"] == 1
    assert state["new_unique_24h"] == 1
    assert state["new_unique_7d"] == 2
    assert 20 <= float(state["oldest_due_age_minutes"]) <= 21
    assert state["catalog_by_kind"]["greenhouse"]["validated"] == 1
    assert state["candidates_by_kind"]["ashby"]["actionable"] == 1


def test_listing_freshness_distinguishes_employer_date_and_discovery_time(tmp_path) -> None:
    database = Database(tmp_path / "listing-freshness.db")
    now = datetime.now(UTC)
    _add_family(database, "recent", activity_at=now - timedelta(hours=3))

    state = listing_freshness_state(database, now=now)

    assert state["active_families"] == 1
    assert state["active_listings"] == 1
    assert state["employer_posted_24h"] == 1
    assert state["found_24h"] == 1
    assert 2.9 <= float(state["newest_visible_activity_age_hours"]) <= 3.1


def test_successful_crawls_do_not_hide_stale_visible_inventory(tmp_path) -> None:
    database = Database(tmp_path / "stale-visible.db")
    now = datetime.now(UTC)
    _add_target(database, "greenhouse:fresh-crawl", complete_at=now)
    _add_family(database, "stale-role", activity_at=now - timedelta(days=3))

    report = production_report(
        database,
        max_activity_minutes=90,
        min_sources=1,
        min_active_listings=1,
        max_listing_activity_hours=24,
    )

    assert report["inventory"]["fresh"] == 1
    assert report["listing_freshness"]["newest_visible_activity_age_hours"] >= 72
    assert report["ok"] is False
    assert any("newest visible listing activity" in error for error in report["errors"])


def test_due_source_candidate_backlog_is_a_production_failure(tmp_path) -> None:
    database = Database(tmp_path / "candidate-stall.db")
    now = datetime.now(UTC)
    _add_target(database, "greenhouse:fresh", complete_at=now)
    _add_family(database, "fresh-role", activity_at=now)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO source_candidates(
                source, kind, scope, spec, origin, next_probe_at
            ) VALUES (
                'ashby:starved', 'ashby', 'current', '{}', 'test',
                now() - interval '4 hours'
            )
            """
        )

    report = production_report(
        database,
        max_activity_minutes=90,
        min_sources=1,
        min_active_listings=1,
        max_candidate_due_minutes=60,
    )

    assert report["source_growth"]["due"] == 1
    assert report["ok"] is False
    assert any("oldest due source candidate" in error for error in report["errors"])
