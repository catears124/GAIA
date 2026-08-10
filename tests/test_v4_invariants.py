from datetime import UTC, datetime, timedelta

import pytest

from gaia.models import Posting
from gaia.v4_invariants import validate_sensor_recall


def _posting(url: str, *, minutes_ago: int | None = None) -> Posting:
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    return Posting(
        company="Acme",
        title="Software Engineering Intern",
        apply_url=url,
        source="sensor:test",
        source_id=url,
        source_mode="market-sensor",
        sensor_reported_at=now - timedelta(minutes=minutes_ago) if minutes_ago is not None else None,
        observed_at=now,
        category="software",
        year=2027,
        season="summer",
        target_match="exact",
    )


def _family(url: str, *, sensor_reported_at: datetime | None = None) -> dict[str, object]:
    return {
        "family_key": "acme::swe",
        "openings": [
            {
                "apply_url": url,
                "sensor_reported_at": sensor_reported_at.isoformat() if sensor_reported_at else None,
            }
        ],
    }


def test_every_sensor_url_must_survive_into_published_feed():
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    postings = [
        _posting("https://jobs.example.com/1"),
        _posting("https://jobs.example.com/2"),
    ]
    families = [_family("https://jobs.example.com/1")]
    with pytest.raises(RuntimeError, match="sensor-to-feed recall"):
        validate_sensor_recall(postings, families, now=now, minimum_recall=1.0)


def test_sensor_timestamp_must_survive_even_when_url_is_present():
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    posting = _posting("https://jobs.example.com/1", minutes_ago=10)
    families = [_family("https://jobs.example.com/1")]
    with pytest.raises(RuntimeError, match="timestamp provenance"):
        validate_sensor_recall([posting], families, now=now, minimum_recall=1.0)


def test_complete_recall_and_timestamp_provenance_pass():
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    posting = _posting("https://jobs.example.com/1?utm_source=tracker", minutes_ago=10)
    reported = now - timedelta(minutes=10)
    families = [_family("https://jobs.example.com/1", sensor_reported_at=reported)]
    result = validate_sensor_recall([posting], families, now=now, minimum_recall=1.0)
    assert result["recall"] == 1.0
    assert result["missing_urls"] == 0
    assert result["timestamp_drops"] == 0
    assert result["recent_timestamped_urls"] == 1
