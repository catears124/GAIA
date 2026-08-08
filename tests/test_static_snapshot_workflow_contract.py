from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(".github/workflows/static-snapshot.yml")


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_snapshot_workflow_has_independent_and_inventory_driven_triggers() -> None:
    text = workflow_text()

    assert 'cron: "3,18,33,48 * * * *"' in text
    assert "workflow_run:" in text
    assert 'workflows: ["Production inventory"]' in text
    assert "types: [completed]" in text


def test_snapshot_exporter_owns_database_checkout_retries() -> None:
    text = workflow_text()

    assert 'GAIA_STATIC_SNAPSHOT_DB_ATTEMPTS: "4"' in text
    assert "exporter_owns_connection_retries" in text
    assert "wait_for_database" not in text
    assert "--json-output database-readiness.json" not in text


def test_snapshot_database_export_only_requires_configuration() -> None:
    text = workflow_text()

    assert "steps.db_gate.outputs.state == 'configured'" in text
    assert "steps.db_gate.outputs.state == 'ready'" not in text
    assert "timeout --signal=TERM --kill-after=10s 170s python -m gaia.static_snapshot" in text


def test_snapshot_workflow_retains_configuration_evidence() -> None:
    text = workflow_text()

    assert "Upload database configuration evidence" in text
    assert "database-readiness.log" in text
    assert "database-readiness.json" in text
    assert "retention-days: 14" in text


def test_snapshot_recovery_uses_published_branch_not_broken_artifact_download() -> None:
    text = workflow_text()

    assert "Restore latest published snapshot" in text
    assert "git show origin/snapshot-data:src/gaia/frontend/last-known-inventory.json" in text
    assert "restore_snapshot_artifact" not in text
    assert "artifact-restored" not in text


def test_snapshot_is_published_without_committing_to_main() -> None:
    text = workflow_text()

    assert "Publish snapshot to data-only branch" in text
    assert "git push origin HEAD:snapshot-data" in text
    assert "git push origin HEAD:main" not in text


def test_old_published_copy_is_not_reported_as_a_fresh_refresh() -> None:
    text = workflow_text()

    assert "Snapshot database configuration is invalid" in text
    assert "Snapshot refresh failed; serving the last published copy" in text
    assert '[ "$source" = published-snapshot ]' in text
    assert 'exit "$fail"' in text
