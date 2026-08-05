from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(".github/workflows/static-snapshot.yml")


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_snapshot_workflow_preserves_database_readiness_states() -> None:
    text = workflow_text()

    assert "--json-output database-readiness.json" in text
    assert "0) state=ready" in text
    assert "1) state=recovering" in text
    assert "2) state=invalid" in text
    assert "*) state=failed" in text
    assert 'echo "state=$state" >> "$GITHUB_OUTPUT"' in text
    assert 'echo "exit_code=$code" >> "$GITHUB_OUTPUT"' in text


def test_snapshot_database_export_requires_exact_ready_state() -> None:
    text = workflow_text()

    assert "steps.db_gate.outputs.state == 'ready'" in text
    assert "steps.db_gate.outcome == 'success'" not in text


def test_snapshot_workflow_retains_readiness_evidence() -> None:
    text = workflow_text()

    assert "Upload database readiness evidence" in text
    assert "database-readiness.log" in text
    assert "database-readiness.json" in text
    assert "retention-days: 14" in text


def test_snapshot_is_published_without_committing_to_main() -> None:
    text = workflow_text()

    assert "Publish snapshot to data-only branch" in text
    assert "git push origin HEAD:snapshot-data" in text
    assert "git push origin HEAD:main" not in text


def test_invalid_or_failed_readiness_is_not_reported_as_recovery() -> None:
    text = workflow_text()

    assert "Snapshot database configuration is invalid" in text
    assert "Snapshot database readiness probe failed internally" in text
    assert 'if [ "$db_state" = invalid ] || [ "$db_state" = failed ]; then' in text
    assert "exit 1" in text
