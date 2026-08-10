from datetime import UTC, datetime, timedelta

from gaia.classify import is_default_target
from gaia.v4_sensors import SensorSpec, parse_sensor, parse_sensor_time


def test_relative_sensor_time_is_not_employer_time():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    parsed, precision = parse_sensor_time("1d", now)
    assert parsed == now - timedelta(days=1)
    assert precision == "day"


def test_speedy_markdown_age_becomes_sensor_evidence():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    body = """
| Company | Position | Location | Salary | Posting | Age |
| --- | --- | --- | --- | --- | --- |
| Google | Software Engineering Intern - BS - Summer 2027 | Mountain View, CA | $72/hr | [Apply](https://careers.google.com/jobs/results/123) | 1d |
"""
    spec = SensorSpec("speedy-test", "markdown", "https://example.test", "summer-2027")
    postings, rows = parse_sensor(spec, body, now)
    assert rows == 1
    assert len(postings) == 1
    posting = postings[0]
    assert is_default_target(posting)
    assert posting.posted_at is None
    assert posting.sensor_reported_at == now - timedelta(days=1)
    assert posting.sensor_reported_raw == "1d"
    assert posting.source_mode == "market-sensor"


def test_summer_2027_feed_confirms_cycle_when_title_omits_year_and_season():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    body = """
| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| Acme | Software Engineering Intern | New York | [Apply](https://jobs.ashbyhq.com/acme/abc) | 2026-08-09 |
"""
    spec = SensorSpec("cycle-test", "markdown", "https://example.test", "summer-2027")
    postings, _ = parse_sensor(spec, body, now)
    assert len(postings) == 1
    posting = postings[0]
    assert posting.target_match == "source_confirmed"
    assert posting.year == 2027
    assert posting.season == "summer"


def test_nuft_role_codes_are_expanded_to_internship_titles():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    body = """
## Jane Street
Role | Links
--- | ---
QR | [Apply](https://www.janestreet.com/join-jane-street/position/123)
"""
    spec = SensorSpec("nuft-test", "markdown", "https://example.test", "summer-2027")
    postings, _ = parse_sensor(spec, body, now)
    assert len(postings) == 1
    assert postings[0].title == "Quantitative Research Intern"
    assert is_default_target(postings[0])


def test_mixed_json_sensor_keeps_only_open_summer_2027():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    body = """
{
  "one": {
    "id": "one",
    "company": "Deepgram",
    "title": "Software Engineering Intern",
    "url": "https://jobs.ashbyhq.com/deepgram/one",
    "location": "Remote",
    "season": "Summer 2027",
    "posted_at": "2026-08-09T10:00:00Z",
    "is_open": true
  },
  "two": {
    "id": "two",
    "company": "Other",
    "title": "Software Engineering Intern - Fall 2026",
    "url": "https://jobs.ashbyhq.com/other/two",
    "season": "Fall 2026",
    "is_open": true
  }
}
"""
    spec = SensorSpec("engine-test", "json-map", "https://example.test", "mixed")
    postings, rows = parse_sensor(spec, body, now)
    assert rows == 2
    assert [posting.company for posting in postings] == ["Deepgram"]
    assert postings[0].sensor_reported_at == datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    assert is_default_target(postings[0])
