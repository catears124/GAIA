from pathlib import Path


WORKFLOW = Path(".github/workflows/inventory.yml")


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_inventory_uses_one_database_readiness_gate() -> None:
    text = workflow_text()
    assert "database_gate:" in text
    assert "Probe database once for the whole workflow" in text
    assert text.count("python -m gaia.wait_for_database --timeout-seconds 600") == 1
    assert "needs: database_gate" in text
    assert "if: needs.database_gate.outputs.state == 'ready'" in text


def test_inventory_does_not_fan_out_during_database_recovery() -> None:
    text = workflow_text()
    assert "state=recovering" in text
    assert "Database recovery active; inventory fanout suppressed" in text
    assert "state=pending" in text
    inventory_block = text.split("\n  inventory:\n", 1)[1].split("\n  status:\n", 1)[0]
    assert "Wait through transient database recovery" not in inventory_block
    assert "--timeout-seconds 600" not in inventory_block


def test_invalid_configuration_is_not_reported_as_recovery() -> None:
    text = workflow_text()
    assert '2) state=invalid ;;' in text
    assert "Production database configuration or credentials are invalid" in text
    assert "state=failure" in text
    assert "exit_code: ${{ steps.gate.outputs.exit_code }}" in text


def test_unexpected_readiness_failure_is_not_reported_as_recovery() -> None:
    text = workflow_text()
    assert '*) state=failed ;;' in text
    assert "Database readiness probe failed unexpectedly" in text


def test_obsolete_inventory_runs_are_cancelled() -> None:
    text = workflow_text()
    assert "group: production-inventory" in text
    assert "cancel-in-progress: true" in text
