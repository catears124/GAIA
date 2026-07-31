from pathlib import Path


WORKFLOW = Path(".github/workflows/severe-recovery.yml")


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_severe_recovery_consumes_machine_readable_readiness_outputs() -> None:
    text = workflow_text()
    assert '--github-output "$GITHUB_OUTPUT"' in text
    assert "--json-output database-readiness.json" in text
    assert "database-readiness.json" in text


def test_severe_recovery_distinguishes_internal_probe_failure() -> None:
    text = workflow_text()
    assert "failed)" in text
    assert "Database readiness probe failed internally" in text
    assert 'invalid|failed|"") exit 1' in text


def test_severe_recovery_suppresses_inventory_work_during_recovery() -> None:
    text = workflow_text()
    assert "if: steps.database.outputs.state == 'ready'" in text
    assert "steps.database.outputs.state == 'ready' && steps.state.outputs.severe == 'true'" in text
    assert "Database recovery active; inventory audit suppressed" in text


def test_severe_recovery_retains_evidence_even_when_probe_fails() -> None:
    text = workflow_text()
    assert "if: always()" in text
    assert "database-readiness.log" in text
    assert "database-readiness.json" in text
    assert "retention-days: 7" in text
