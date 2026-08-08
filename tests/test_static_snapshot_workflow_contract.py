from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(".github/workflows/static-snapshot.yml")
SNAPSHOT = Path("src/gaia/static_snapshot.py")
FAST_SNAPSHOT = Path("src/gaia/static_snapshot_fast.py")


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_snapshot_workflow_has_independent_schedule_without_inventory_spam() -> None:
    text = workflow_text()

    assert 'cron: "3,18,33,48 * * * *"' in text
    assert "workflow_dispatch:" in text
    assert 'workflows: ["Production inventory"]' not in text
    assert "workflow_run:" not in text


def test_snapshot_workflow_never_restarts_an_inflight_refresh() -> None:
    text = workflow_text()

    assert "group: gaia-static-inventory-snapshot-v3" in text
    assert "cancel-in-progress: false" in text


def test_snapshot_exporter_owns_database_checkout_retries() -> None:
    text = workflow_text()
    fast = FAST_SNAPSHOT.read_text(encoding="utf-8")

    assert 'GAIA_STATIC_SNAPSHOT_DB_ATTEMPTS: "4"' in text
    assert "job_exporter_owns_connection_retries" in text
    assert "GAIA_STATIC_SNAPSHOT_DB_ATTEMPTS" in fast
    assert "wait_for_database" not in text
    assert "--json-output database-readiness.json" not in text


def test_snapshot_family_reads_are_bounded_keyset_pages() -> None:
    workflow = workflow_text()
    code = SNAPSHOT.read_text(encoding="utf-8")

    assert 'GAIA_STATIC_SNAPSHOT_FAMILY_PAGE_SIZE: "256"' in workflow
    assert 'GAIA_STATIC_SNAPSHOT_FAMILY_PAGE_SIZE", "256"' in code
    assert "WHERE family_key > %s" in code
    assert "ORDER BY family_key" in code
    assert "LIMIT %s" in code
    assert "OFFSET" not in code.split("def _direct_family_index", 1)[1].split(
        "def _responses_from_index", 1
    )[0]
    assert "COALESCE(latest_posted_at, first_detected_at) DESC" not in code.split(
        "def _direct_family_index", 1
    )[1].split("def _responses_from_index", 1)[0]


def test_snapshot_publication_is_decoupled_from_crawler_health() -> None:
    workflow = workflow_text()
    fast = FAST_SNAPSHOT.read_text(encoding="utf-8")

    assert "python -m gaia.static_snapshot_fast" in workflow
    assert "_inventory_state_from_connection" not in fast
    assert "live_health" not in fast
    assert "_snapshot_health" in fast
    assert 'health["stale"] = True' in fast
    assert 'inventory["stale_snapshot"] = True' in fast


def test_snapshot_database_export_only_requires_configuration() -> None:
    text = workflow_text()

    assert "steps.db_gate.outputs.state == 'configured'" in text
    assert "steps.db_gate.outputs.state == 'ready'" not in text
    assert "timeout --signal=TERM --kill-after=10s 170s python -m gaia.static_snapshot_fast" in text


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


def test_snapshot_does_not_call_the_public_api_as_a_circular_fallback() -> None:
    text = workflow_text()

    assert "static_snapshot_http" not in text
    assert "GAIA_PUBLIC_BASE_URL" not in text
    assert "Export through public API" not in text


def test_snapshot_is_published_without_committing_to_main() -> None:
    text = workflow_text()

    assert "Publish snapshot to data-only branch" in text
    assert "git push origin HEAD:snapshot-data" in text
    assert "git push origin HEAD:main" not in text


def test_old_published_copy_is_not_reported_as_a_fresh_refresh() -> None:
    text = workflow_text()

    assert "Snapshot database configuration is invalid" in text
    assert "Fresh job snapshot export failed; serving last published copy" in text
    assert 'elif [ "$source" != database ]' in text
    assert 'exit "$fail"' in text
