from __future__ import annotations

from gaia.collectors import LeverCollector
from gaia.db import Database
from gaia.dynamic_market_discovery import candidate_collectors
from gaia.models import Posting
from gaia.source_catalog import save_candidates


def test_dynamic_market_leads_enter_candidate_queue_not_validated_catalog(tmp_path) -> None:
    posting = Posting(
        company="Example",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://jobs.lever.co/example/12345678-1234-1234-1234-123456789abc",
        source="market-index:github:community/internships-2027",
        source_id="example-2027",
        source_mode="external-index",
    )

    candidates = candidate_collectors([posting], {})

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
    postings = [
        Posting(
            company="Example",
            title=f"Software Engineer Intern {index}, Summer 2027",
            apply_url=f"https://jobs.lever.co/example/{index:08d}-1234-1234-1234-123456789abc",
            source="market-index:github:community/internships-2027",
            source_id=str(index),
            source_mode="external-index",
        )
        for index in range(3)
    ]

    candidates = candidate_collectors(postings, {})

    assert len(candidates) == 1
    assert candidates[0].name == "lever:example"
