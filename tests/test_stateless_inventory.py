from __future__ import annotations

from datetime import UTC, datetime

from gaia.classify import classify
from gaia.models import Posting
from gaia.stateless_inventory import collectors_from_census, merge_refresh


def test_collectors_from_census_shards_complete_feed_candidates() -> None:
    snapshot = {
        "candidates": [
            {"source": "domain:isolvedhire:acme"},
            {"source": "domain:jazzhr:beta"},
            {"source": "domain:other:ignored"},
        ]
    }
    selected = []
    for shard in range(4):
        selected.extend(
            collectors_from_census(snapshot, shards=4, shard_index=shard)
        )
    assert sorted(item.name for item in selected) == [
        "isolvedhire:acme",
        "jazzhr:beta",
    ]


def test_merge_refresh_replaces_only_refreshed_source_aliases() -> None:
    previous = {
        "family_index": [
            {
                "family_key": "old-family",
                "title": "Old Internship",
                "company": "Old Co",
                "category": "software",
                "target_match": "exact",
                "year": 2027,
                "season": "summer",
                "locations": ["Remote"],
                "openings": [
                    {
                        "apply_url": "https://acme.isolvedhire.com/jobs/old",
                        "source": "domain:isolvedhire:acme",
                        "source_mode": "direct",
                        "location": ["Remote"],
                        "posted_at": "2026-07-01T00:00:00+00:00",
                        "first_detected_at": "2026-07-02T00:00:00+00:00",
                    },
                    {
                        "apply_url": "https://boards.greenhouse.io/keep/jobs/1",
                        "source": "greenhouse:keep",
                        "source_mode": "direct",
                        "location": ["New York, NY"],
                        "posted_at": "2026-07-03T00:00:00+00:00",
                        "first_detected_at": "2026-07-04T00:00:00+00:00",
                    },
                ],
            }
        ]
    }
    posting = classify(
        Posting(
            company="Acme",
            title="Software Engineering Intern Summer 2027",
            apply_url="https://acme.isolvedhire.com/jobs/new",
            source="isolvedhire:acme",
            source_id="new",
            locations=["Remote"],
            source_mode="direct",
            posted_at=datetime(2026, 8, 9, tzinfo=UTC),
            posted_precision="date",
            posted_confidence="official",
        )
    )
    merged = merge_refresh(
        previous,
        postings=[posting],
        refreshed_aliases={"isolvedhire:acme", "domain:isolvedhire:acme"},
        now="2026-08-09T22:00:00+00:00",
    )
    openings = [opening for family in merged for opening in family["openings"]]
    urls = {opening["apply_url"] for opening in openings}
    assert "https://acme.isolvedhire.com/jobs/old" not in urls
    assert "https://acme.isolvedhire.com/jobs/new" in urls
    assert "https://boards.greenhouse.io/keep/jobs/1" in urls
    assert any(family["quality"] == "verified" for family in merged)
