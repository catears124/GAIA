from datetime import UTC, datetime, timedelta

from gaia.static_snapshot import _family_page_from_index, _filter_family_index


def _family(
    key: str,
    *,
    posted: datetime | None,
    found: datetime,
    verified: datetime,
) -> dict[str, object]:
    return {
        "family_key": key,
        "title": key,
        "company": "Example",
        "category": "software",
        "target_match": "exact",
        "locations": [],
        "verified": True,
        "latest_posted_at": posted.isoformat() if posted else None,
        "first_detected_at": found.isoformat(),
        "last_verified_at": verified.isoformat(),
    }


def test_newest_snapshot_sort_does_not_promote_old_jobs_discovered_today() -> None:
    now = datetime.now(UTC)
    genuinely_new = _family(
        "new-posting",
        posted=now - timedelta(hours=4),
        found=now - timedelta(hours=3),
        verified=now - timedelta(hours=1),
    )
    old_but_just_discovered = _family(
        "old-recovery",
        posted=now - timedelta(days=35),
        found=now - timedelta(minutes=5),
        verified=now - timedelta(minutes=2),
    )

    page = _family_page_from_index([old_but_just_discovered, genuinely_new], sort="newest")

    assert [item["family_key"] for item in page["items"]] == [
        "new-posting",
        "old-recovery",
    ]


def test_recency_filter_uses_discovery_only_when_employer_date_is_missing() -> None:
    now = datetime.now(UTC)
    old_but_just_discovered = _family(
        "old-recovery",
        posted=now - timedelta(days=35),
        found=now - timedelta(minutes=5),
        verified=now,
    )
    undated_new_discovery = _family(
        "undated-new",
        posted=None,
        found=now - timedelta(hours=2),
        verified=now,
    )

    filtered = _filter_family_index(
        [old_but_just_discovered, undated_new_discovery],
        posted_within=7,
    )

    assert [item["family_key"] for item in filtered] == ["undated-new"]
