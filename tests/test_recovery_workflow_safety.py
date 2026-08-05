from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_severe_recovery_suppresses_inventory_audit_during_database_recovery():
    text = workflow("severe-recovery.yml")

    assert "python -m gaia.wait_for_database" in text
    assert "GAIA_DATABASE_URL: ${{ secrets.POSTGRES_URL }}" in text
    assert "steps.database.outputs.state == 'ready' && steps.state.outputs.severe == 'true'" in text
    assert "Database recovery active; inventory audit suppressed" in text
    assert "cancel-in-progress: true" in text


def test_source_audit_checks_database_once_before_matrix_fanout():
    text = workflow("source-audit.yml")

    assert "name: Verify database before audit fanout" in text
    assert text.count("python -m gaia.wait_for_database") == 1
    assert "if: needs.gate.outputs.database_state == 'ready'" in text
    assert "Database recovery active; source fanout suppressed" in text
    assert "max-parallel: 4" in text


def test_recovery_workflows_never_use_direct_non_pooling_connection():
    for name in ("severe-recovery.yml", "source-audit.yml"):
        text = workflow(name)
        assert "POSTGRES_URL_NON_POOLING" not in text
        assert "secrets.POSTGRES_URL" in text
