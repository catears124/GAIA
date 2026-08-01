from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gaia.collectors import LeverCollector
from gaia.db import Database
from gaia.dynamic_market_discovery import (
    SNAPSHOT_VERSION,
    candidate_collectors,
    deserialize_candidates,
    posting_freshness,
    serialize_candidates,
)
from gaia.models import Posting
from gaia.source_catalog import save_candidates


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


def test_dynamic_market_leads_enter_candidate_queue_not_validated_catalog(tmp_path) -> None:
    candidates = candidate_collectors([example_posting()], {})

    assert len(candidates) == 1
    assert isinstance(candidates[0], LeverCollector)
    assert candidates[0].name == "lever:example"

    database = Database(tmp_path / "gaia.db")
    assert save_candidates(database, candidates, origin="dynamic-github-market") == 1

    with database.connect() as connection:
        candidate = connection.execute(
            "SELECT source, status, origin FROM source_candidates"
        ).fetchone()
        catalog_count = connection.execute(
            "SELECT COUNT(*) AS count FROM source_catalog"
        ).fetchone()

    assert candidate["source"] == "lever:example"
    assert candidate["status"] == "candidate"
    assert candidate["origin"] == "dynamic-github-market"
    assert catalog_count["count"] == 0


def test_dynamic_market_discovery_deduplicates_repeated_provider_evidence() -> None:
    candidates = candidate_collectors([example_posting(index) for index in range(3)], {})

    assert len(candidates) == 1
    assert candidates[0].name == "lever:example"


def test_dynamic_market_snapshot_round_trip_preserves_probe_only_source() -> None:
    candidates = candidate_collectors([example_posting()], {})
    serialized = serialize_candidates(candidates)
    restored = deserialize_candidates(serialized)

    assert SNAPSHOT_VERSION == 1
    assert serialized == [
        {
            "source": "lever:example",
            "kind": "lever",
            "scope": "current",
            "spec": {"company": "Example", "site": "example"},
        }
    ]
    assert len(restored) == 1
    assert isinstance(restored[0], LeverCollector)
    assert restored[0].name == "lever:example"
    assert restored[0].scope == "current"


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

    assert [collector.name for collector in restored] == ["lever:example"]


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
