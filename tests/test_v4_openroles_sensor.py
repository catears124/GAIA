from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta

from gaia.v4_openroles_sensor import _candidate_chunks, _decode_chunk, _rows_to_postings


def test_candidate_chunks_stop_after_recent_window():
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    manifest = {
        "slim_index_chunks": [
            {
                "file": "slim/slim-0000-a.json.gz",
                "posted_max": now.isoformat().replace("+00:00", "Z"),
            },
            {
                "file": "slim/slim-0001-b.json.gz",
                "posted_max": (now - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
            },
            {
                "file": "slim/slim-0002-c.json.gz",
                "posted_max": (now - timedelta(days=10)).isoformat().replace("+00:00", "Z"),
            },
        ]
    }
    selected = _candidate_chunks(manifest, cutoff=now - timedelta(days=7), limit=6)
    assert [row["file"] for row in selected] == [
        "slim/slim-0000-a.json.gz",
        "slim/slim-0001-b.json.gz",
    ]


def test_decode_chunk_accepts_precompressed_static_payload():
    rows = [{"i": "1", "c": "Acme", "ti": "Software Engineering Intern", "u": "https://example.test/job"}]
    encoded = gzip.compress(json.dumps(rows).encode())
    assert _decode_chunk(encoded) == rows


def test_openroles_rows_preserve_freshness_as_sensor_evidence_not_employer_evidence():
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    posted = now - timedelta(hours=3)
    rows = [
        {
            "i": "abc",
            "a": "ashby",
            "ti": "Software Engineering Intern",
            "c": "Acme",
            "r": 0,
            "s": 0,
            "loc": "New York, NY",
            "p": posted.isoformat().replace("+00:00", "Z"),
            "f": (posted + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            "u": "https://jobs.ashbyhq.com/acme/abc",
        }
    ]
    postings = _rows_to_postings(rows, fetched_at=now, cutoff=now - timedelta(days=7))
    assert len(postings) == 1
    posting = postings[0]
    assert posting.source == "sensor:openroles-recent"
    assert posting.source_mode == "market-sensor"
    assert posting.posted_at is None
    assert posting.sensor_reported_at == posted
    assert posting.canonical_apply_url == "https://jobs.ashbyhq.com/acme/abc"


def test_openroles_sensor_drops_stale_recruiter_and_cold_rows():
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    recent = now.isoformat().replace("+00:00", "Z")
    old = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    rows = [
        {"i": "stale", "c": "A", "ti": "Software Intern", "u": "https://a.test/1", "s": 1, "p": recent},
        {"i": "recruiter", "c": "B", "ti": "Software Intern", "u": "https://b.test/1", "r": 1, "p": recent},
        {"i": "old", "c": "C", "ti": "Software Intern", "u": "https://c.test/1", "p": old},
    ]
    assert _rows_to_postings(rows, fetched_at=now, cutoff=now - timedelta(days=7)) == []
