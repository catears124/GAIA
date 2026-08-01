from pathlib import Path


def test_conversion_funnel_workflow_archives_actionable_evidence() -> None:
    workflow = Path(".github/workflows/conversion-funnel.yml").read_text(encoding="utf-8")

    assert 'cron: "13 * * * *"' in workflow
    assert "/api/maintenance/diagnostics/conversion" in workflow
    assert "/api/maintenance/diagnostics/drain-candidates" in workflow
    assert "github.event_name != 'workflow_dispatch'" in workflow
    assert "concurrency=12" in workflow
    assert "new_verified_jobs_window" in workflow
    assert "candidate_sources_due" in workflow
    assert "verified_postings_missing_family" in workflow
    assert "diagnostic_errors" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "gaia/verified-job-conversion" in workflow
