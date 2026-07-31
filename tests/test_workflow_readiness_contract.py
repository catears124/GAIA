from pathlib import Path


WORKFLOWS = {
    "inventory": Path(".github/workflows/inventory.yml"),
    "maintenance": Path(".github/workflows/maintenance.yml"),
    "reconcile": Path(".github/workflows/reconcile.yml"),
}


def workflow(name: str) -> str:
    return WORKFLOWS[name].read_text(encoding="utf-8")


def test_all_database_gates_preserve_wait_exit_codes() -> None:
    for name in WORKFLOWS:
        text = workflow(name)
        assert "set +e" in text, name
        assert "code=$?" in text, name
        assert "set -e" in text, name
        assert '0) state=ready ;;' in text or "0)\n              state=ready" in text, name
        assert '1) state=recovering ;;' in text or "1)\n              state=recovering" in text, name
        assert '2) state=invalid ;;' in text or "2)\n              state=invalid" in text, name
        assert '*) state=failed ;;' in text or "*)\n              state=failed" in text, name
        assert 'echo "exit_code=$code" >> "$GITHUB_OUTPUT"' in text, name


def test_recovery_suppresses_expensive_work_without_claiming_success() -> None:
    inventory = workflow("inventory")
    maintenance = workflow("maintenance")
    reconcile = workflow("reconcile")

    assert "if: needs.database_gate.outputs.state == 'ready'" in inventory
    assert "Database recovery active; inventory fanout suppressed" in inventory

    assert "if: needs.readiness.outputs.state == 'ready'" in maintenance
    assert "Database recovery active; production maintenance suppressed" in maintenance

    assert "if: steps.gate.outputs.state == 'ready'" in reconcile
    assert "Database recovery active; reconciliation suppressed" in reconcile


def test_invalid_credentials_are_hard_failures_in_status_reporting() -> None:
    expected = {
        "inventory": "Production database configuration or credentials are invalid",
        "maintenance": "Production maintenance database configuration or credentials are invalid",
        "reconcile": "Production read-model database configuration or credentials are invalid",
    }
    for name, description in expected.items():
        text = workflow(name)
        assert description in text
        invalid_block = text.split("= invalid", 1)[1]
        assert "state=failure" in invalid_block


def test_unexpected_probe_errors_are_distinct_from_recovery() -> None:
    for name in WORKFLOWS:
        text = workflow(name)
        assert "probe failed unexpectedly" in text.lower(), name
