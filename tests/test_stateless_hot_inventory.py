from __future__ import annotations

from datetime import UTC, datetime

from gaia.classify import classify
from gaia.models import Posting
from gaia.stateless_hot_inventory import (
    _direct_target,
    _source_confirms_summer_2027,
    merge_mixed_refresh,
    snapshot_seed_postings,
)


def _posting(
    *,
    source: str,
    source_mode: str,
    url: str,
    title: str = "Software Engineer Intern",
    company: str = "Example",
) -> Posting:
    return classify(
        Posting(
            company=company,
            title=title,
            apply_url=url,
            source=source,
            source_id=url,
            locations=["New York, NY"],
            source_mode=source_mode,
        )
    )


def test_simplify_registry_is_cycle_evidence_for_generic_intern_titles() -> None:
    posting = _posting(
        source="registry:simplify-2027",
        source_mode="registry",
        url="https://job-boards.greenhouse.io/example/jobs/1",
    )
    assert posting.target_match == "unknown"
    promoted = _source_confirms_summer_2027(posting)
    assert promoted.target_match == "source_confirmed"
    assert promoted.year == 2027
    assert promoted.season == "summer"


def test_registry_lead_stays_unverified_in_snapshot() -> None:
    previous = {"family_index": []}
    posting = _source_confirms_summer_2027(
        _posting(
            source="registry:simplify-2027",
            source_mode="registry",
            url="https://job-boards.greenhouse.io/example/jobs/1",
        )
    )
    merged = merge_mixed_refresh(
        previous,
        postings=[posting],
        refreshed_aliases={"registry:simplify-2027"},
        now="2026-08-09T23:30:00+00:00",
    )
    assert len(merged) == 1
    assert merged[0]["quality"] == "lead"
    assert merged[0]["verified"] is False
    assert merged[0]["direct_openings"] == 0


def test_employer_observation_replaces_same_url_registry_lead() -> None:
    lead = _source_confirms_summer_2027(
        _posting(
            source="registry:simplify-2027",
            source_mode="registry",
            url="https://job-boards.greenhouse.io/example/jobs/1?utm_source=registry",
        )
    )
    first = {
        "family_index": merge_mixed_refresh(
            {"family_index": []},
            postings=[lead],
            refreshed_aliases={"registry:simplify-2027"},
            now="2026-08-09T23:30:00+00:00",
        )
    }
    direct = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern - Summer 2027",
            apply_url="https://job-boards.greenhouse.io/example/jobs/1",
            source="greenhouse:example",
            source_id="1",
            locations=["New York, NY"],
            source_mode="direct",
            posted_at=datetime(2026, 8, 9, tzinfo=UTC),
            posted_precision="timestamp",
            posted_confidence="official",
        )
    )
    merged = merge_mixed_refresh(
        first,
        postings=[direct],
        refreshed_aliases={"greenhouse:example"},
        now="2026-08-09T23:40:00+00:00",
    )
    openings = [opening for family in merged for opening in family["openings"]]
    assert len(openings) == 1
    assert openings[0]["source"] == "greenhouse:example"
    assert openings[0]["source_mode"] == "direct"
    assert any(family["verified"] for family in merged)


def test_direct_unknown_cycle_inherits_matching_registry_cycle_only() -> None:
    lead = _source_confirms_summer_2027(
        _posting(
            source="registry:simplify-2027",
            source_mode="registry",
            url="https://jobs.lever.co/example/abc",
        )
    )
    direct = _posting(
        source="lever:example",
        source_mode="direct",
        url="https://jobs.lever.co/example/abc",
    )
    assert direct.target_match == "unknown"
    promoted = _direct_target(direct, {lead.canonical_apply_url: lead})
    assert promoted.target_match == "source_confirmed"
    assert promoted.year == 2027
    assert promoted.season == "summer"


def test_snapshot_seed_postings_preserve_known_application_urls() -> None:
    snapshot = {
        "family_index": [
            {
                "company": "Example",
                "title": "Software Engineer Intern",
                "locations": ["Remote"],
                "openings": [
                    {
                        "apply_url": "https://jobs.ashbyhq.com/example/abc",
                        "location": ["Remote"],
                    }
                ],
            }
        ]
    }
    postings = snapshot_seed_postings(snapshot)
    assert len(postings) == 1
    assert postings[0].apply_url == "https://jobs.ashbyhq.com/example/abc"
    assert postings[0].source_mode == "registry"
