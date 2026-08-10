from datetime import UTC, datetime, timedelta

from gaia.models import Posting
from gaia.v4_pipeline import Observation, _build_families


def test_sensor_and_employer_observations_fuse_without_conflating_timestamps():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    url = "https://jobs.ashbyhq.com/acme/abc"
    sensor = Posting(
        company="Acme",
        title="Software Engineering Intern - Summer 2027",
        apply_url=url,
        source="sensor:test",
        source_id=url,
        source_mode="market-sensor",
        sensor_reported_at=now - timedelta(hours=2),
        observed_at=now,
        category="software",
        season="summer",
        year=2027,
        target_match="exact",
    )
    employer = Posting(
        company="Acme",
        title="Software Engineering Intern - Summer 2027",
        apply_url=url,
        source="ashby:acme",
        source_id="abc",
        source_mode="direct",
        posted_at=now - timedelta(days=1),
        posted_precision="timestamp",
        posted_confidence="official",
        observed_at=now,
        category="software",
        season="summer",
        year=2027,
        target_match="exact",
    )
    families = _build_families(
        [
            Observation(sensor, first_seen_at=now),
            Observation(employer, first_seen_at=now, verified_at=now),
        ]
    )
    assert len(families) == 1
    family = families[0]
    assert family["verified"] is True
    assert family["direct_openings"] == 1
    assert family["backstop_openings"] == 0
    assert family["market_event_kind"] == "employer-posted"
    opening = family["openings"][0]
    assert opening["evidence_count"] == 2
    assert opening["sensor_reported_at"] == (now - timedelta(hours=2)).isoformat()
    assert opening["posted_at"] == (now - timedelta(days=1)).isoformat()


def test_unverified_sensor_stays_a_lead():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    posting = Posting(
        company="Acme",
        title="Machine Learning Intern - Summer 2027",
        apply_url="https://careers.acme.test/job/1",
        source="sensor:test",
        source_id="1",
        source_mode="market-sensor",
        sensor_reported_at=now - timedelta(minutes=30),
        observed_at=now,
        category="ml-ai",
        season="summer",
        year=2027,
        target_match="exact",
    )
    family = _build_families([Observation(posting, first_seen_at=now)])[0]
    assert family["verified"] is False
    assert family["direct_openings"] == 0
    assert family["backstop_openings"] == 1
    assert family["market_event_kind"] == "sensor-reported"
