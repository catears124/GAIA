from pathlib import Path

from gaia.provider_health_summary import summarize


WORKFLOW = Path(".github/workflows/production-health.yml")


def test_provider_summary_aggregates_backlog_truthfully() -> None:
    report = summarize(
        [
            {"kind": "greenhouse", "total": 10, "fresh": 8, "due": 2, "unhealthy": 2, "running": 1, "degraded": 1},
            {"kind": "lever", "total": 5, "fresh": 5, "due": 0, "unhealthy": 0, "running": 0, "degraded": 0},
        ]
    )
    assert report["ok"] is False
    assert report["total"] == 15
    assert report["fresh"] == 13
    assert report["unhealthy"] == 2
    assert report["fresh_percent"] == 86.7


def test_production_health_preserves_readiness_contract() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '--github-output "$GITHUB_OUTPUT"' in text
    assert "--json-output database-readiness.json" in text
    assert "steps.database.outputs.state == 'ready'" in text
    assert "database-invalid" in text
    assert "database-failed" in text
    assert "database-missing" in text


def test_provider_backlog_failure_is_not_hidden() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "provider-health-summary.json" in text
    assert 'reason = "provider backlog checker failed"' in text
    assert "provider_unhealthy" in text
    assert "provider_due" in text


def test_recovery_only_dispatches_for_inventory_degradation() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "if: steps.recovery_gate.outputs.state == 'recover'" in text
    assert "Database recovery is in progress" in text
    assert "Production database configuration is invalid" in text
