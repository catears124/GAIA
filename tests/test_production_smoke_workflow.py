from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "production-api-smoke.yml"
EVALUATOR = ROOT / "src" / "gaia" / "production_smoke.py"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def evaluator_text() -> str:
    return EVALUATOR.read_text(encoding="utf-8")


def test_production_smoke_runs_frequently_without_overlap() -> None:
    text = workflow_text()
    assert 'cron: "*/15 * * * *"' in text
    assert "group: production-api-smoke" in text
    assert "cancel-in-progress: true" in text


def test_frontend_resilience_changes_trigger_production_smoke() -> None:
    text = workflow_text()
    for path in (
        "src/gaia/frontend/index.html",
        "src/gaia/frontend/remote-snapshot.js",
        "src/gaia/frontend/api-resilience.js",
        "src/gaia/frontend/emergency-outage.js",
        "src/gaia/frontend/outage-controller.js",
        "src/gaia/production_smoke.py",
    ):
        assert path in text


def test_production_smoke_fetches_the_deployed_resilience_chain() -> None:
    text = workflow_text()
    assert "fetch index /" in text
    assert "fetch remote '/assets/remote-snapshot.js?v=1.0.1'" in text
    assert "fetch resilience '/assets/api-resilience.js?v=2.0.0'" in text
    assert "fetch emergency '/assets/emergency-outage.js?v=2.0.0'" in text
    assert "fetch controller '/assets/outage-controller.js?v=1.2.1'" in text
    assert "raw.githubusercontent.com/catears124/GAIA/snapshot-data" in text
    assert "python -m gaia.production_smoke" in text


def test_production_smoke_fails_active_legacy_cache_and_stale_snapshot() -> None:
    text = evaluator_text()
    assert '"window.fetch =" in emergency.body' in text
    assert '"localStorage" in emergency.body' in text
    assert '"durable device backup" in emergency.body' in text
    assert "MAX_PUBLIC_SNAPSHOT_AGE = timedelta(minutes=45)" in text
    assert "Published inventory snapshot is missing, invalid, or older than 45 minutes" in text


def test_production_smoke_never_calls_unhealthy_inventory_healthy() -> None:
    text = evaluator_text()
    assert "Health API dishonestly reports ok with unhealthy inventory" in text
    assert "Health API reports contradictory inventory state" in text
    assert "Production reachable; inventory catch-up" in text


def test_production_smoke_preserves_evidence_and_fails_bad_contracts() -> None:
    text = workflow_text()
    assert "actions/upload-artifact@v4" in text
    assert "retention-days: 14" in text
    assert "gaia/production-smoke" in text
    assert 'test "$state" != failure' in text
