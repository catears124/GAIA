from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gaia.production_coverage import evaluate_coverage

NOW = datetime(2026, 7, 31, 23, 0, tzinfo=UTC)


def health(*, ok: bool, total: int = 6, fresh: int = 5, unhealthy: int = 1):
    return {
        "ok": ok,
        "stale": False,
        "inventory": {"total": total, "fresh": fresh, "unhealthy": unhealthy},
    }


def universe(**summary_overrides: object) -> dict[str, object]:
    summary: dict[str, object] = {
        "known_employers": 12,
        "enumerated_employers": 5,
        "unresolved_employers": 7,
        "blind_spots": 3,
    }
    summary.update(summary_overrides)
    return {"ready": True, "summary": summary, "frontier": []}


def source(name: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": name,
        "enabled": True,
        "crawl_status": "ok",
        "last_complete_at": (NOW - timedelta(minutes=5)).isoformat(),
        "interval_seconds": 1200,
        "lease_expires_at": None,
    }
    payload.update(overrides)
    return payload


def test_never_completed_source_is_named_and_keeps_status_pending() -> None:
    report = evaluate_coverage(
        health(ok=False),
        {"sources": [source("ready"), source("new-board", last_complete_at=None)]},
        universe(),
        now=NOW,
    )

    assert report.state == "pending"
    assert report.description.startswith("Inventory catch-up 5/6; 1 source needs attention")
    assert "12 employers covered" in report.description
    assert report.attention[0].source == "new-board"
    assert report.attention[0].reason == "never_completed"


def test_broken_and_overdue_sources_are_distinguished() -> None:
    report = evaluate_coverage(
        health(ok=False, total=3, fresh=1, unhealthy=2),
        {
            "sources": [
                source("broken", crawl_status="broken"),
                source(
                    "overdue",
                    last_complete_at=(NOW - timedelta(hours=2)).isoformat(),
                    interval_seconds=1200,
                ),
            ]
        },
        universe(),
        now=NOW,
    )

    assert report.state == "pending"
    assert {(item.source, item.reason) for item in report.attention} == {
        ("broken", "broken"),
        ("overdue", "overdue"),
    }


def test_active_lease_is_reported_as_running_overdue_not_hidden() -> None:
    report = evaluate_coverage(
        health(ok=False, total=1, fresh=0, unhealthy=1),
        {
            "sources": [
                source(
                    "running",
                    last_complete_at=(NOW - timedelta(hours=2)).isoformat(),
                    lease_expires_at=(NOW + timedelta(minutes=5)).isoformat(),
                )
            ]
        },
        universe(),
        now=NOW,
    )

    assert report.state == "pending"
    assert report.attention[0].reason == "overdue_running"


def test_aggregate_unhealthy_count_cannot_hide_unnamed_sources() -> None:
    report = evaluate_coverage(
        health(ok=False),
        {"sources": [source("ready")]},
        universe(),
        now=NOW,
    )

    assert report.state == "failure"
    assert "omitted 1 unhealthy configured source" in report.description


def test_green_health_with_unhealthy_sources_fails_closed() -> None:
    report = evaluate_coverage(
        health(ok=True),
        {"sources": [source("broken", crawl_status="broken")]},
        universe(),
        now=NOW,
    )

    assert report.state == "failure"
    assert "reports ok" in report.description


def test_all_fresh_sources_and_populated_universe_are_successful() -> None:
    report = evaluate_coverage(
        health(ok=True, total=2, fresh=2, unhealthy=0),
        {"sources": [source("one"), source("two")]},
        universe(),
        now=NOW,
    )

    assert report.state == "success"
    assert report.description == (
        "All 2 configured sources are fresh; 12 employers covered; "
        "5 enumerated, 7 unresolved, 3 blind spots"
    )


def test_empty_or_unready_employer_universe_fails_closed() -> None:
    empty = evaluate_coverage(
        health(ok=True, total=1, fresh=1, unhealthy=0),
        {"sources": [source("ready")]},
        universe(
            known_employers=0,
            enumerated_employers=0,
            unresolved_employers=0,
            blind_spots=0,
        ),
        now=NOW,
    )
    unready = evaluate_coverage(
        health(ok=True, total=1, fresh=1, unhealthy=0),
        {"sources": [source("ready")]},
        {"ready": False, "reason": "empty_read_model", "summary": {}, "frontier": []},
        now=NOW,
    )

    assert empty.state == "failure"
    assert empty.description == "Employer universe contains no employers"
    assert unready.state == "failure"
    assert "empty_read_model" in unready.description


def test_rebuild_required_employer_gap_fails_closed() -> None:
    payload = universe(coverage_gap_employers=4)
    payload["rebuild_required"] = True
    report = evaluate_coverage(
        health(ok=True, total=1, fresh=1, unhealthy=0),
        {"sources": [source("ready")]},
        payload,
        now=NOW,
    )

    assert report.state == "failure"
    assert report.description == (
        "Employer universe requires rebuild; 4 evidence-backed employers are missing"
    )


def test_invalid_or_duplicate_evidence_fails_closed() -> None:
    assert evaluate_coverage({}, {}, {}, now=NOW).state == "failure"
    report = evaluate_coverage(
        health(ok=False),
        {"sources": [source("same"), source("same")]},
        universe(),
        now=NOW,
    )
    assert report.state == "failure"


def test_contradictory_employer_counts_fail_closed() -> None:
    report = evaluate_coverage(
        health(ok=True, total=1, fresh=1, unhealthy=0),
        {"sources": [source("ready")]},
        universe(known_employers=10, enumerated_employers=5, unresolved_employers=6),
        now=NOW,
    )

    assert report.state == "failure"
    assert "contradictory" in report.description
