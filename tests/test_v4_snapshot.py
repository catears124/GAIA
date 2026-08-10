from datetime import UTC, datetime, timedelta

from gaia.v4_snapshot import family_page, stats


def _family(
    key: str,
    *,
    hours_ago: int,
    verified: bool,
    event_kind: str = "first-seen",
    year: int | None = 2027,
    season: str | None = "summer",
    target_match: str = "exact",
) -> dict[str, object]:
    event = datetime.now(UTC) - timedelta(hours=hours_ago)
    return {
        "family_key": key,
        "title": f"{key} Software Engineering Intern",
        "company": key,
        "category": "software",
        "target_match": target_match,
        "year": year,
        "season": season,
        "locations": ["New York"],
        "opening_count": 1,
        "direct_openings": 1 if verified else 0,
        "backstop_openings": 0 if verified else 1,
        "verified": verified,
        "market_event_at": event.isoformat(),
        "market_event_kind": event_kind,
        "latest_posted_at": event.isoformat() if event_kind == "employer-posted" else None,
        "latest_sensor_reported_at": event.isoformat() if event_kind == "sensor-reported" else None,
        "market_first_seen_at": event.isoformat(),
        "first_detected_at": event.isoformat(),
        "last_verified_at": event.isoformat() if verified else None,
        "remote": False,
        "openings": [],
    }


def test_newest_sort_does_not_bury_fresh_lead_below_old_verified_job():
    fresh_lead = _family("Fresh", hours_ago=1, verified=False)
    old_verified = _family("Old", hours_ago=72, verified=True)
    page = family_page([old_verified, fresh_lead], sort="newest")
    assert [item["family_key"] for item in page["items"]] == ["Fresh", "Old"]


def test_verified_filter_remains_strict():
    fresh_lead = _family("Fresh", hours_ago=1, verified=False)
    old_verified = _family("Old", hours_ago=72, verified=True)
    page = family_page([old_verified, fresh_lead], sort="newest", trust="verified")
    assert [item["family_key"] for item in page["items"]] == ["Old"]


def test_posted_within_requires_external_date_not_gaia_discovery_time():
    discovered_now = _family("FoundNow", hours_ago=1, verified=True, event_kind="first-seen")
    reported_now = _family("ReportedNow", hours_ago=2, verified=True, event_kind="sensor-reported")
    page = family_page([discovered_now, reported_now], trust="verified", posted_within=1)
    assert [item["family_key"] for item in page["items"]] == ["ReportedNow"]


def test_summer_2027_filter_uses_cycle_metadata_not_classifier_label():
    scoped = _family(
        "Scoped",
        hours_ago=1,
        verified=False,
        year=2027,
        season="summer",
        target_match="source_confirmed",
    )
    spring = _family(
        "Spring",
        hours_ago=1,
        verified=False,
        year=2027,
        season="spring",
        target_match="wrong_season",
    )
    page = family_page([scoped, spring], target="exact")
    assert [item["family_key"] for item in page["items"]] == ["Scoped"]


def test_any_2027_filter_keeps_spring_and_summer_2027():
    spring = _family(
        "Spring",
        hours_ago=1,
        verified=False,
        year=2027,
        season="spring",
        target_match="wrong_season",
    )
    summer = _family(
        "Summer",
        hours_ago=2,
        verified=False,
        year=2027,
        season="summer",
        target_match="source_confirmed",
    )
    fall_2026 = _family(
        "Fall2026",
        hours_ago=3,
        verified=False,
        year=2026,
        season="fall",
        target_match="wrong_season",
    )
    page = family_page([fall_2026, summer, spring], target="default")
    assert {item["family_key"] for item in page["items"]} == {"Spring", "Summer"}


def test_new_verified_24h_means_recent_external_market_date_not_recent_crawl():
    recent_verified = _family("Recent", hours_ago=4, verified=True, event_kind="employer-posted")
    old_verified = _family("Old", hours_ago=96, verified=True, event_kind="employer-posted")
    fresh_lead = _family("Lead", hours_ago=2, verified=False, event_kind="sensor-reported")
    found_verified = _family("Found", hours_ago=1, verified=True, event_kind="first-seen")
    result = stats([recent_verified, old_verified, fresh_lead, found_verified])
    assert result["new_verified_24h"] == 1
    assert result["new_today"] == 1
    assert result["market_events_24h"] == 3
    assert result["dated_market_events_24h"] == 2
    assert result["employer_posted_24h"] == 1
    assert result["sensor_reported_24h"] == 1
    assert result["first_seen_only_24h"] == 1


def test_first_seen_only_is_not_misreported_as_source_dated_freshness():
    discovered = _family("Discovered", hours_ago=1, verified=False, event_kind="first-seen")
    result = stats([discovered])
    assert result["market_events_24h"] == 1
    assert result["dated_market_events_24h"] == 0
    assert result["employer_posted_24h"] == 0
    assert result["sensor_reported_24h"] == 0
    assert result["first_seen_only_24h"] == 1
