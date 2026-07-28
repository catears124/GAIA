from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected block not found in {path}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "src/gaia/product_api.py",
    '''    # Employer-published chronology is the trustworthy primary order. Within the
    # same published time, timestamp precision beats day precision. Roles whose
    # employer did not publish a date fall back to GAIA's first-detected time.
    return (
        "(latest_posted_at IS NOT NULL) DESC, "
        "latest_posted_at DESC NULLS LAST, "
        "CASE posted_precision WHEN 'timestamp' THEN 0 WHEN 'day' THEN 1 ELSE 2 END, "
        "first_detected_at DESC, last_verified_at DESC, family_key"
    )
''',
    '''    # Sort by the newest trustworthy activity. A newly discovered role must not
    # be buried beneath every role that happens to expose an older employer date.
    # When activity times tie, employer dates and precise timestamps win.
    return (
        "GREATEST(COALESCE(latest_posted_at, first_detected_at), first_detected_at) DESC, "
        "(latest_posted_at IS NOT NULL) DESC, "
        "CASE posted_precision WHEN 'timestamp' THEN 0 WHEN 'day' THEN 1 ELSE 2 END, "
        "first_detected_at DESC, last_verified_at DESC, family_key"
    )
''',
)

health_path = Path("src/gaia/health.py")
health_text = health_path.read_text(encoding="utf-8")
start = health_text.index("def inventory_state(")
end = health_text.index("\n\ndef production_report(", start)
new_health = '''FRESHNESS_FLOOR_SECONDS = 90 * 60
FRESHNESS_INTERVAL_MULTIPLIER = 3


def inventory_state(database: Database) -> dict[str, Any]:
    """Return mutually exclusive source-health counts for the public inventory."""
    with database.connect() as connection:
        row = connection.execute(
            """
            WITH current_targets AS (
                SELECT
                    target.*,
                    GREATEST(target.interval_seconds * %s, %s) AS freshness_seconds
                FROM crawl_targets AS target
                JOIN source_catalog AS catalog USING(source)
                WHERE target.enabled
                  AND target.scheduled
                  AND catalog.validated
                  AND catalog.scope='current'
            )
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE target.lease_expires_at > now()
                ) AS running,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NULL
                ) AS never_completed,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NOT NULL
                      AND target.last_complete_at <
                          now() - make_interval(secs => target.freshness_seconds)
                ) AS overdue,
                COUNT(*) FILTER (
                    WHERE target.last_status = ANY(%s)
                ) AS degraded,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NOT NULL
                      AND target.last_complete_at >=
                          now() - make_interval(secs => target.freshness_seconds)
                      AND target.last_status <> ALL(%s)
                ) AS fresh,
                COUNT(*) FILTER (
                    WHERE target.last_complete_at IS NULL
                       OR target.last_complete_at <
                          now() - make_interval(secs => target.freshness_seconds)
                       OR target.last_status = ANY(%s)
                ) AS unhealthy,
                MAX(target.last_finished_at) AS latest_activity_at,
                MIN(target.last_complete_at) FILTER (
                    WHERE target.last_complete_at IS NOT NULL
                ) AS coverage_watermark
            FROM current_targets AS target
            """,
            (
                FRESHNESS_INTERVAL_MULTIPLIER,
                FRESHNESS_FLOOR_SECONDS,
                list(BAD_STATUSES),
                list(BAD_STATUSES),
                list(BAD_STATUSES),
            ),
        ).fetchone()
        historical = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM crawl_targets AS target
            JOIN source_catalog AS catalog USING(source)
            WHERE target.scheduled
              AND catalog.validated
              AND catalog.scope='historical'
            """
        ).fetchone()

    state: dict[str, Any] = {key: row[key] for key in row.keys()}
    for key in (
        "total",
        "running",
        "never_completed",
        "overdue",
        "degraded",
        "fresh",
        "unhealthy",
    ):
        state[key] = int(state.get(key) or 0)
    state["historical"] = int(historical["count"] or 0)
    state["latest_activity_at"] = iso(state.get("latest_activity_at"))
    state["coverage_watermark"] = iso(state.get("coverage_watermark"))
    state["freshness_floor_seconds"] = FRESHNESS_FLOOR_SECONDS
    total = int(state["total"])
    state["fresh_percent"] = round(100 * int(state["fresh"]) / total, 1) if total else 0.0
    state["healthy"] = bool(total) and int(state["unhealthy"]) == 0
    return state
'''
health_path.write_text(health_text[:start] + new_health + health_text[end:], encoding="utf-8")

replace(
    "src/gaia/frontend/index.html",
    '''          <h1>Technical internships.</h1>
          <p>Direct employer applications first. Leads are visible, but never passed off as verified.</p>''',
    '''          <h1>get a job you silly larp</h1>
          <p>constantly updating cs internships</p>''',
)
replace(
    "src/gaia/frontend/index.html",
    '''          <span id="result-note">Employer-posted date first; GAIA detection time is the fallback.</span>''',
    '''          <span id="result-note">Newest employer-posted or GAIA-found activity first.</span>''',
)
replace(
    "src/gaia/frontend/index.html",
    '''  <script src="/assets/app-v2.js?v=8.0.0" defer></script>''',
    '''  <script src="/assets/app-v2.js?v=8.1.0" defer></script>''',
)

replace(
    "src/gaia/frontend/app-v2.js",
    '''  const primaryDate = item.latest_posted_at
    ? `Posted ${relative(item.latest_posted_at, item.posted_precision)}`
    : `Found ${relative(item.first_detected_at)}`;
  const secondaryDate = item.latest_posted_at
    ? `Found ${relative(item.first_detected_at)}`
    : `Checked ${relative(item.last_verified_at)}`;
''',
    '''  const postedAt = item.latest_posted_at ? new Date(item.latest_posted_at).getTime() : Number.NaN;
  const foundAt = item.first_detected_at ? new Date(item.first_detected_at).getTime() : Number.NaN;
  const foundIsNewer = Number.isFinite(foundAt) && (!Number.isFinite(postedAt) || foundAt > postedAt);
  const primaryTimestamp = foundIsNewer
    ? item.first_detected_at
    : (item.latest_posted_at || item.first_detected_at);
  const primaryDate = foundIsNewer
    ? `Found ${relative(item.first_detected_at)}`
    : `Posted ${relative(item.latest_posted_at, item.posted_precision)}`;
  const secondaryDate = item.latest_posted_at
    ? foundIsNewer
      ? `Employer posted ${relative(item.latest_posted_at, item.posted_precision)}`
      : `Found ${relative(item.first_detected_at)}`
    : `Checked ${relative(item.last_verified_at)}`;
''',
)
replace(
    "src/gaia/frontend/app-v2.js",
    '''    <div class="job-date"><strong title="${esc(exact(item.latest_posted_at || item.first_detected_at))}">${esc(primaryDate)}</strong><span>${esc(secondaryDate)}</span></div>''',
    '''    <div class="job-date"><strong title="${esc(exact(primaryTimestamp))}">${esc(primaryDate)}</strong><span>${esc(secondaryDate)}</span></div>''',
)

replace(
    "tests/test_employer_universe.py",
    '''    assert order.startswith("(latest_posted_at IS NOT NULL) DESC")
    assert "latest_posted_at DESC NULLS LAST" in order
    assert "CASE posted_precision" in order
    assert order.index("latest_posted_at DESC") < order.index("CASE posted_precision")
    assert order.index("CASE posted_precision") < order.index("first_detected_at DESC")
''',
    '''    assert order.startswith("GREATEST(COALESCE(latest_posted_at")
    assert "(latest_posted_at IS NOT NULL) DESC" in order
    assert "CASE posted_precision" in order
    assert order.index("GREATEST") < order.index("(latest_posted_at IS NOT NULL) DESC")
    assert order.index("(latest_posted_at IS NOT NULL) DESC") < order.index("CASE posted_precision")
    assert order.index("CASE posted_precision") < order.index("first_detected_at DESC")
''',
)

replace(
    ".github/workflows/production-health.yml",
    '''permissions:
  contents: read
  statuses: write
''',
    '''permissions:
  actions: write
  contents: read
  statuses: write
''',
)
replace(
    ".github/workflows/production-health.yml",
    '''      - name: Upload health report
        if: always()
        uses: actions/upload-artifact@v4
''',
    '''      - name: Dispatch recovery crawl when inventory is stale
        if: steps.production_check.outcome != 'success'
        shell: bash
        run: |
          active=$(gh run list --workflow inventory.yml --limit 20 --json status \\
            --jq '[.[] | select(.status == "in_progress" or .status == "queued" or .status == "waiting" or .status == "requested" or .status == "pending")] | length')
          if [ "$active" -eq 0 ]; then
            gh workflow run inventory.yml --ref main -f budget_seconds=900
            echo "Dispatched a bounded production inventory recovery run." >> "$GITHUB_STEP_SUMMARY"
          else
            echo "A production inventory run is already active; no duplicate recovery was queued." >> "$GITHUB_STEP_SUMMARY"
          fi
      - name: Upload health report
        if: always()
        continue-on-error: true
        uses: actions/upload-artifact@v4
''',
)

replace(
    ".github/workflows/inventory.yml",
    '''on:
  schedule:
    - cron: "2,17,32,47 * * * *"
''',
    '''on:
  push:
    branches: [main]
    paths:
      - ".github/workflows/inventory.yml"
      - "src/gaia/health.py"
  schedule:
    - cron: "2,17,32,47 * * * *"
''',
)
replace(
    ".github/workflows/inventory.yml",
    '''      - name: Upload health report
        if: always()
        uses: actions/upload-artifact@v4
''',
    '''      - name: Upload health report
        if: always()
        continue-on-error: true
        uses: actions/upload-artifact@v4
''',
)

Path("tests/test_recency_health.py").write_text(
    '''from __future__ import annotations

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
''',
    encoding="utf-8",
)
