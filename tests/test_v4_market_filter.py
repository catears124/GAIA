from datetime import UTC, datetime

from gaia.models import Posting
from gaia.v4_market_filter import is_current_market_target, normalize_sensor_postings


def _posting(
    title: str,
    *,
    url: str = "https://jobs.ashbyhq.com/acme/1",
    year: int | None = None,
    season: str | None = None,
    target_match: str = "unknown",
    category: str = "software",
) -> Posting:
    return Posting(
        company="Acme",
        title=title,
        apply_url=url,
        source="sensor:active-market",
        source_id=url,
        source_mode="market-sensor",
        observed_at=datetime(2026, 8, 10, tzinfo=UTC),
        year=year,
        season=season,
        target_match=target_match,
        category=category,
    )


def test_fall_2026_technical_internship_is_current_market_inventory():
    posting = _posting(
        "Software Engineering Intern - Fall 2026",
        year=2026,
        season="fall",
        target_match="wrong_season",
    )
    assert is_current_market_target(posting, now=datetime(2026, 8, 10, tzinfo=UTC))


def test_spring_2027_technical_internship_is_current_market_inventory():
    posting = _posting(
        "Software Engineering Intern - Spring 2027",
        year=2027,
        season="spring",
        target_match="wrong_season",
    )
    assert is_current_market_target(posting, now=datetime(2026, 8, 10, tzinfo=UTC))


def test_summer_2027_remains_current_and_filterable():
    posting = _posting(
        "Software Engineering Intern - Summer 2027",
        year=2027,
        season="summer",
        target_match="exact",
    )
    assert is_current_market_target(posting, now=datetime(2026, 8, 10, tzinfo=UTC))


def test_old_explicit_cycle_is_not_current_market_inventory():
    posting = _posting(
        "Software Engineering Intern - Summer 2025",
        year=2025,
        season="summer",
        target_match="wrong_year",
    )
    assert not is_current_market_target(posting, now=datetime(2026, 8, 10, tzinfo=UTC))


def test_non_internship_and_nontechnical_rows_are_rejected():
    non_intern = _posting("Software Engineer", target_match="not_internship")
    nontechnical = _posting("Marketing Intern", target_match="unknown", category="other")
    assert not is_current_market_target(non_intern, now=datetime(2026, 8, 10, tzinfo=UTC))
    assert not is_current_market_target(nontechnical, now=datetime(2026, 8, 10, tzinfo=UTC))


def test_normalization_does_not_discard_current_off_cycle_rows():
    posting = _posting(
        "Software Engineering Intern - Fall 2026",
        year=2026,
        season="fall",
        target_match="wrong_season",
    )
    assert normalize_sensor_postings([posting]) == [posting]


def test_trailing_quote_is_removed_from_raw_url():
    postings = normalize_sensor_postings(
        [
            _posting(
                "Software Engineering Intern - Summer 2027",
                url="https://jobs.ashbyhq.com/acme/1\"",
                year=2027,
                season="summer",
                target_match="exact",
            )
        ]
    )
    assert postings[0].apply_url == "https://jobs.ashbyhq.com/acme/1"
