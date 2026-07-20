from __future__ import annotations

from gaia.classify import classify
from gaia.db import Database
from gaia.models import CollectorResult, Posting, canonical_url


def target_posting(*, source: str = "registry:test", source_mode: str = "registry") -> Posting:
    return classify(
        Posting(
            company="Example",
            title="Software Engineer Intern, Summer 2027",
            apply_url="https://example.com/jobs/123?utm_source=tracker",
            source=source,
            source_id="123",
            source_mode=source_mode,
        ),
        source_confirms_2027=source_mode == "registry",
    )


def test_coverage_uses_only_latest_completed_run_sources(tmp_path):
    db = Database(tmp_path / "gaia.db")

    old_run = db.start_run()
    db.record_failure(
        CollectorResult(
            source="schema:old.example:Old",
            postings=[],
            complete=False,
            mode="verification",
            rows_scanned=0,
            error="old failure",
            status="broken",
        ),
        run_id=old_run,
    )
    db.finish_run(old_run, sources=1, postings=0, failed=1)

    new_run = db.start_run()
    db.apply_result(
        CollectorResult(
            source="greenhouse:example",
            postings=[],
            complete=True,
            mode="board",
            rows_scanned=1,
            expected_rows=1,
            status="ok",
        ),
        rebuild=False,
        run_id=new_run,
    )
    db.finish_run(new_run, sources=1, postings=0, failed=0)

    coverage = db.coverage()
    assert coverage["contract"]["run_id"] == new_run
    assert [source["source"] for source in coverage["sources"]] == ["greenhouse:example"]
    assert coverage["contract"]["actionable_anomalies"] == 0


def test_closed_verification_page_removes_stale_registry_application(tmp_path):
    db = Database(tmp_path / "gaia.db")
    run_id = db.start_run()
    posting = target_posting()
    db.apply_result(
        CollectorResult(
            source="registry:test",
            postings=[posting],
            complete=True,
            mode="registry",
            rows_scanned=1,
            expected_rows=1,
            status="loaded",
        ),
        rebuild=False,
        run_id=run_id,
    )
    db.apply_result(
        CollectorResult(
            source="schema:example.com:Example",
            postings=[],
            complete=False,
            mode="verification",
            rows_scanned=1,
            expected_rows=1,
            status="stale",
            note="1 stale/closed page",
            closed_urls=[canonical_url(posting.apply_url)],
        ),
        rebuild=False,
        run_id=run_id,
    )
    db.rebuild_families()
    db.finish_run(run_id, sources=2, postings=1, failed=0)

    coverage = db.coverage()
    assert coverage["summary"]["registry_floor"] == 0
    assert coverage["summary"]["registry_only"] == 0
    assert coverage["contract"]["stale_verifications"] == 1
    assert db.list_families()["total"] == 0


def test_expected_limitations_do_not_count_as_actionable_failures(tmp_path):
    db = Database(tmp_path / "gaia.db")
    run_id = db.start_run()
    db.apply_result(
        CollectorResult(
            source="schema:blocked.example:Example",
            postings=[],
            complete=False,
            mode="verification",
            rows_scanned=1,
            status="blocked",
            scope="current",
            note="1 access-blocked page",
        ),
        rebuild=False,
        run_id=run_id,
    )
    db.apply_result(
        CollectorResult(
            source="ashby:historical-example",
            postings=[],
            complete=True,
            mode="board",
            rows_scanned=0,
            expected_rows=0,
            status="dormant",
            scope="historical",
            note="historical watch board currently exposes zero jobs",
        ),
        rebuild=False,
        run_id=run_id,
    )
    db.finish_run(run_id, sources=2, postings=0, failed=0)

    contract = db.coverage()["contract"]
    assert contract["actionable_anomalies"] == 0
    assert contract["access_limited"] == 1
    assert contract["dormant_watches"] == 1


def test_current_broken_and_empty_boards_are_actionable(tmp_path):
    db = Database(tmp_path / "gaia.db")
    run_id = db.start_run()
    db.record_failure(
        CollectorResult(
            source="greenhouse:bad",
            postings=[],
            complete=False,
            mode="board",
            rows_scanned=0,
            error="404",
            status="broken",
            scope="current",
        ),
        run_id=run_id,
    )
    db.apply_result(
        CollectorResult(
            source="ashby:empty-current",
            postings=[],
            complete=True,
            mode="board",
            rows_scanned=0,
            expected_rows=0,
            status="empty",
            scope="current",
            note="current board returned zero jobs",
        ),
        rebuild=False,
        run_id=run_id,
    )
    db.finish_run(run_id, sources=2, postings=0, failed=1)

    contract = db.coverage()["contract"]
    assert contract["actionable_anomalies"] == 2
