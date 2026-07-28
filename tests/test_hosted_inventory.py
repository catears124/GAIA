from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gaia.db import Database
from gaia.health import inventory_state, production_report
from gaia.live_inventory import LiveDatabase, LiveInventoryStore


def add_target(
    database: Database,
    source: str,
    *,
    kind: str = "greenhouse",
    complete_at: datetime | None = None,
    status: str = "pending",
) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO source_catalog(source, kind, scope, spec, validated, origin)
            VALUES (%s,%s,'current','{}'::jsonb,TRUE,'test')
            """,
            (source, kind),
        )
        connection.execute(
            """
            INSERT INTO crawl_targets(
                source, enabled, scheduled, priority, interval_seconds,
                next_run_at, last_complete_at, last_finished_at, last_status
            ) VALUES (%s,TRUE,TRUE,10,900,now(),%s,%s,%s)
            """,
            (source, complete_at, complete_at, status),
        )


def add_family(database: Database) -> None:
    now = datetime.now(UTC)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO families(
                family_key, company, title, category, season, year, target_match,
                opening_count, location_count, locations, openings, posted_precision,
                first_detected_at, last_verified_at, direct_openings, backstop_openings
            ) VALUES (
                'example:software-intern', 'Example', 'Software Engineer Intern',
                'software', 'summer', 2027, 'exact', 1, 1, ARRAY['Remote'],
                '[]'::jsonb, 'day', %s, %s, 1, 0
            )
            """,
            (now, now),
        )


def test_parallel_stores_claim_distinct_sources(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "claims.db")
    add_target(database, "greenhouse:first")
    add_target(database, "greenhouse:second")
    monkeypatch.setenv("GAIA_WORKER_KINDS", "greenhouse")

    first_store = LiveInventoryStore(database, "worker-a")
    second_store = LiveInventoryStore(database, "worker-b")
    first = first_store.claim_target(lease_seconds=300)
    second = second_store.claim_target(lease_seconds=300)

    assert first is not None
    assert second is not None
    assert first.source != second.source
    assert first_store.claim_target(lease_seconds=300) is None


def test_provider_lane_only_claims_allowed_kind(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "lanes.db")
    add_target(database, "greenhouse:example", kind="greenhouse")
    add_target(database, "workday:example:external", kind="workday-search")
    monkeypatch.setenv("GAIA_WORKER_KINDS", "workday-search")

    store = LiveInventoryStore(database, "workday-worker")
    target = store.claim_target(lease_seconds=300)

    assert target is not None
    assert target.kind == "workday-search"
    assert target.source == "workday:example:external"
    assert store.claim_target(lease_seconds=300) is None


def test_pipeline_database_commits_and_returns_rows(tmp_path) -> None:
    database = LiveDatabase(tmp_path / "pipeline.db")
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO source_catalog(source, kind, scope, spec, validated, origin)
            VALUES ('greenhouse:pipeline','greenhouse','current','{}'::jsonb,TRUE,'test')
            """
        )
        row = connection.execute(
            "SELECT source FROM source_catalog WHERE source='greenhouse:pipeline'"
        ).fetchone()
        assert row["source"] == "greenhouse:pipeline"

    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM source_catalog WHERE source='greenhouse:pipeline'"
        ).fetchone()
    assert count["count"] == 1


def test_health_union_does_not_double_count_overdue_degraded_source(tmp_path) -> None:
    database = Database(tmp_path / "health.db")
    now = datetime.now(UTC)
    add_target(database, "greenhouse:fresh", complete_at=now, status="ok")
    add_target(
        database,
        "greenhouse:old-broken",
        complete_at=now - timedelta(hours=2),
        status="broken",
    )

    state = inventory_state(database)

    assert state["total"] == 2
    assert state["fresh"] == 1
    assert state["overdue"] == 1
    assert state["degraded"] == 1
    assert state["unhealthy"] == 1
    assert state["fresh_percent"] == 50.0
    assert state["healthy"] is False


def test_production_checker_accepts_recent_populated_inventory(tmp_path) -> None:
    database = Database(tmp_path / "checker-ok.db")
    add_target(database, "greenhouse:healthy", complete_at=datetime.now(UTC), status="ok")
    add_family(database)

    report = production_report(
        database,
        max_activity_minutes=90,
        min_sources=1,
        min_active_listings=1,
    )

    assert report["ok"] is True
    assert report["errors"] == []
    assert report["inventory"]["fresh"] == 1
    assert report["product"]["active_listings"] == 1


def test_production_checker_rejects_stalled_inventory(tmp_path) -> None:
    database = Database(tmp_path / "checker-stale.db")
    add_target(
        database,
        "greenhouse:stale",
        complete_at=datetime.now(UTC) - timedelta(hours=3),
        status="ok",
    )
    add_family(database)

    report = production_report(
        database,
        max_activity_minutes=90,
        min_sources=1,
        min_active_listings=1,
    )

    assert report["ok"] is False
    assert any("activity is" in error for error in report["errors"])
    assert "no validated current source is fresh" in report["errors"]
