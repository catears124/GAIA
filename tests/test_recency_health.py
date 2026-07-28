from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from gaia.db import Database
from gaia.health import FRESHNESS_FLOOR_SECONDS, inventory_state
from gaia.product_api import _live_order_clause


def add_target(database: Database, source: str, *, complete_at: datetime) -> None:
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


def test_newest_sort_uses_latest_activity_before_date_quality() -> None:
    order = _live_order_clause("newest")
    assert order.startswith("GREATEST(COALESCE(latest_posted_at")
    assert order.index("GREATEST") < order.index("(latest_posted_at IS NOT NULL) DESC")


def test_inventory_freshness_has_scheduler_safe_floor(tmp_path) -> None:
    database = Database(tmp_path / "freshness-floor.db")
    add_target(
        database,
        "greenhouse:recent",
        complete_at=datetime.now(UTC) - timedelta(minutes=45),
    )

    state = inventory_state(database)

    assert FRESHNESS_FLOOR_SECONDS == 90 * 60
    assert state["fresh"] == 1
    assert state["overdue"] == 0


def test_inventory_still_rejects_genuinely_stale_sources(tmp_path) -> None:
    database = Database(tmp_path / "genuinely-stale.db")
    add_target(
        database,
        "greenhouse:stale",
        complete_at=datetime.now(UTC) - timedelta(hours=2),
    )

    state = inventory_state(database)

    assert state["fresh"] == 0
    assert state["overdue"] == 1


def test_product_copy_and_recency_display_are_shipped() -> None:
    frontend = Path(__file__).parents[1] / "src" / "gaia" / "frontend"
    html = (frontend / "index.html").read_text(encoding="utf-8")
    script = (frontend / "app-v2.js").read_text(encoding="utf-8")

    assert "get a job you silly larp" in html
    assert "constantly updating cs internships" in html
    assert "Newest employer-posted or GAIA-found activity first." in html
    assert "foundIsNewer" in script
    assert "Employer posted ${relative" in script


def test_health_watchdog_can_dispatch_recovery() -> None:
    root = Path(__file__).parents[1]
    health_workflow = (root / ".github" / "workflows" / "production-health.yml").read_text(encoding="utf-8")
    inventory_workflow = (root / ".github" / "workflows" / "inventory.yml").read_text(encoding="utf-8")

    assert "actions: write" in health_workflow
    assert "gh workflow run inventory.yml" in health_workflow
    assert "continue-on-error: true" in health_workflow
    assert '"src/gaia/health.py"' in inventory_workflow
