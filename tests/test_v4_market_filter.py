from datetime import UTC, datetime

from gaia.models import Posting
from gaia.v4_market_filter import normalize_sensor_postings


def _posting(title: str, url: str = "https://jobs.ashbyhq.com/acme/1") -> Posting:
    return Posting(
        company="Acme",
        title=title,
        apply_url=url,
        source="sensor:summer-2027",
        source_id=url,
        source_mode="market-sensor",
        observed_at=datetime(2026, 8, 10, tzinfo=UTC),
        year=2027,
        season="summer",
        target_match="source_confirmed",
    )


def test_explicit_fall_role_is_rejected_even_from_summer_feed():
    assert normalize_sensor_postings([_posting("Software Engineering Intern - Fall 2026")]) == []


def test_combined_fall_2026_summer_2027_role_is_kept():
    postings = normalize_sensor_postings(
        [_posting("Software Engineering Internship - Fall 2026 / Summer 2027")]
    )
    assert len(postings) == 1


def test_explicit_2026_without_2027_is_rejected():
    assert normalize_sensor_postings([_posting("Quantitative Intern 2026")]) == []


def test_trailing_quote_is_removed_from_raw_url():
    postings = normalize_sensor_postings(
        [_posting("Software Engineering Intern - Summer 2027", "https://jobs.ashbyhq.com/acme/1\"")]
    )
    assert postings[0].apply_url == "https://jobs.ashbyhq.com/acme/1"
