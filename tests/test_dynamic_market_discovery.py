from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gaia.collectors import LeverCollector
from gaia.dynamic_market_discovery import (
    SNAPSHOT_VERSION,
    candidate_collectors,
    deserialize_candidates,
    posting_freshness,
    serialize_candidates,
)
from gaia.models import Posting


def example_posting(index: int = 0) -> Posting:
    return Posting(
        company="Example",
        title=f"Software Engineer Intern {index}, Summer 2027",
        apply_url=(
            f"https://jobs.lever.co/example/{index:08d}-1234-1234-1234-123456789abc"
        ),
        source="market-index:github:community/internships-2027",
        source_id=str(index),
        source_mode="external-index",
    )


def test_dynamic_market_leads_become_probe_only_source_candidates() -> None:
    candidates = candidate_collectors([example_posting()], {})
    serialized = serialize_candidates(candidates)
    by_source = {row["source"]: row for row in serialized}

    assert isinstance(
        next(collector for collector in candidates if collector.name == "lever:example"),
        LeverCollector,
    )
    assert by_source["lever:example"] == {
        "source": "lever:example",
        "kind": "lever",
        "scope": "current",
        "spec": {"company": "Example", "site": "example"},
    }
    assert by_source["google-careers"] == {
        "source": "google-careers",
        "kind": "google-careers",
        "scope": "current",
        "spec": {},
    }
    assert all(row["kind"] != "verification" for row in serialized)


def test_dynamic_market_discovery_deduplicates_repeated_provider_evidence() -> None:
    candidates = candidate_collectors([example_posting(index) for index in range(3)], {})

    assert {collector.name for collector in candidates} == {
        "lever:example",
        "google-careers",
    }


def test_dynamic_market_snapshot_round_trip_preserves_probe_only_sources() -> None:
    candidates = candidate_collectors([example_posting()], {})
    serialized = serialize_candidates(candidates)
    restored = deserialize_candidates(serialized)

    assert SNAPSHOT_VERSION == 1
    assert {collector.name for collector in restored} == {
        "lever:example",
        "google-careers",
    }
    lever = next(collector for collector in restored if collector.name == "lever:example")
    assert isinstance(lever, LeverCollector)
    assert lever.scope == "current"


def test_dynamic_market_snapshot_rejects_unsupported_or_duplicate_rows() -> None:
    rows = serialize_candidates(candidate_collectors([example_posting()], {}))
    restored = deserialize_candidates(
        rows
        + rows
        + [
            {"source": "verification:unsafe", "kind": "verification", "spec": {}},
            {"source": "lever:broken", "kind": "lever", "spec": {}},
        ]
    )

    assert {collector.name for collector in restored} == {
        "lever:example",
        "google-careers",
    }


def test_posting_freshness_counts_only_trusted_employer_dates() -> None:
    now = datetime.now(UTC)
    postings = [example_posting(index) for index in range(6)]
    postings[0].posted_at = now - timedelta(hours=2)
    postings[1].posted_at = now - timedelta(days=2)
    postings[2].posted_at = now - timedelta(days=6)
    postings[3].posted_at = now - timedelta(days=12)
    for posting in postings[:4]:
        posting.posted_confidence = "official"

    # Workday-style relative labels are useful evidence, but must not masquerade as
    # precise employer timestamps in freshness metrics.
    postings[4].posted_at = now
    postings[4].posted_confidence = "approximate"
    postings[5].posted_at = None

    summary = posting_freshness(postings)

    assert summary["dated_postings"] == 4
    assert summary["untrusted_dated_postings"] == 1
    assert summary["employer_posted_last_24h"] == 1
    assert summary["employer_posted_last_72h"] == 2
    assert summary["employer_posted_last_7d"] == 3
    assert summary["freshest_employer_posted_at"] == postings[0].posted_at.isoformat()
