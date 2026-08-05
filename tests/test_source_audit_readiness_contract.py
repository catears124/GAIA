from pathlib import Path

WORKFLOW = Path(".github/workflows/source-audit.yml")


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_gate_exports_complete_readiness_contract() -> None:
    text = workflow_text()
    assert "database_state: ${{ steps.database.outputs.state }}" in text
    assert "database_exit_code: ${{ steps.database.outputs.exit_code }}" in text
    assert "database_reason: ${{ steps.database.outputs.reason }}" in text
    assert '--github-output "$GITHUB_OUTPUT"' in text
    assert "--json-output database-readiness.json" in text


def test_audit_fanout_requires_exact_ready_state() -> None:
    text = workflow_text()
    assert "if: needs.gate.outputs.database_state == 'ready'" in text
    assert "Database recovery active; source fanout suppressed" in text


def test_internal_probe_failure_is_not_reported_as_recovery() -> None:
    text = workflow_text()
    assert "Database readiness probe failed internally" in text
    assert "Database readiness produced no valid state" in text
    assert "if [ \"$state\" = failure ]; then" in text


def test_readiness_evidence_is_retained() -> None:
    text = workflow_text()
    assert "database-readiness.log" in text
    assert "database-readiness.json" in text
    assert "retention-days: 7" in text
