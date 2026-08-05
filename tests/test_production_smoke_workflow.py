from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "production-api-smoke.yml"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_production_smoke_runs_frequently_without_overlap() -> None:
    text = workflow_text()
    assert 'cron: "*/15 * * * *"' in text
    assert "group: production-api-smoke" in text
    assert "cancel-in-progress: true" in text


def test_production_smoke_checks_ui_and_core_read_contracts() -> None:
    text = workflow_text()
    assert "fetch index /" in text
    assert "fetch health /api/health" in text
    assert "fetch stats /api/stats" in text
    assert "fetch families '/api/families?page=1&page_size=12&sort=newest'" in text
    assert "grep -q 'api-resilience.js'" in text
    assert '(.items | type == "array") and (.total | type == "number")' in text


def test_production_smoke_never_calls_unhealthy_inventory_healthy() -> None:
    text = workflow_text()
    assert '.ok == true and ((.inventory.healthy // false) != true)' in text
    assert "Health API dishonestly reports ok with unhealthy inventory" in text
    assert "state=pending" in text
    assert "Production database/API recovery active" in text


def test_production_smoke_preserves_evidence_and_fails_bad_contracts() -> None:
    text = workflow_text()
    assert "actions/upload-artifact@v4" in text
    assert "retention-days: 14" in text
    assert "gaia/production-smoke" in text
    assert 'test "$state" != failure' in text
