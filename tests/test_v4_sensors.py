from datetime import UTC, datetime, timedelta

from gaia.classify import is_default_target
from gaia.v4_sensors import SensorSpec, parse_sensor, parse_sensor_time


def test_relative_sensor_time_is_not_employer_time():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    parsed, precision = parse_sensor_time("1d", now)
    assert parsed == now - timedelta(days=1)
    assert precision == "day"


def test_unix_sensor_timestamp_is_parsed_as_utc():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    epoch = int(datetime(2026, 8, 9, 10, 30, tzinfo=UTC).timestamp())
    parsed, precision = parse_sensor_time(epoch, now)
    assert parsed == datetime(2026, 8, 9, 10, 30, tzinfo=UTC)
    assert precision == "timestamp"


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


def test_html_registry_rows_are_ingested():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    body = """
<table>
  <tr><th>Company</th><th>Role</th><th>Location</th><th>Apply</th><th>Added</th></tr>
  <tr><td>Acme</td><td>Machine Learning Intern</td><td>New York</td><td><a href="https://jobs.ashbyhq.com/acme/ml">Apply</a></td><td>2026-08-09</td></tr>
</table>
"""
    spec = SensorSpec("html-test", "markdown", "https://example.test", "summer-2027")
    postings, rows = parse_sensor(spec, body, now)
    assert rows == 1
    assert len(postings) == 1
    assert postings[0].company == "Acme"
    assert postings[0].target_match == "source_confirmed"
    assert postings[0].sensor_reported_at == datetime(2026, 8, 9, tzinfo=UTC)


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


def test_multisource_terms_array_confirms_summer_2027_and_epoch_time():
    now = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)
    posted = datetime(2026, 8, 9, 21, 15, tzinfo=UTC)
    body = f"""
[
  {{
    "source": "Aggregator",
    "company_name": "Acme",
    "id": "summer",
    "title": "Software Engineer Intern",
    "active": true,
    "terms": ["Summer 2027"],
    "date_posted": {int(posted.timestamp())},
    "url": "https://jobs.ashbyhq.com/acme/summer",
    "locations": ["New York"],
    "is_visible": true
  }},
  {{
    "source": "Aggregator",
    "company_name": "Acme",
    "id": "fall",
    "title": "Software Engineer Intern",
    "active": true,
    "terms": ["Fall 2026"],
    "date_posted": {int(posted.timestamp())},
    "url": "https://jobs.ashbyhq.com/acme/fall",
    "locations": ["New York"],
    "is_visible": true
  }}
]
"""
    spec = SensorSpec("multisource-test", "json-list", "https://example.test", "mixed")
    postings, rows = parse_sensor(spec, body, now)
    assert rows == 2
    assert len(postings) == 1
    posting = postings[0]
    assert posting.source_id == "summer"
    assert posting.year == 2027
    assert posting.season == "summer"
    assert posting.target_match == "source_confirmed"
    assert posting.sensor_reported_at == posted
