from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from gaia.cli import _render_json


def test_health_report_serializes_nested_dates_and_datetimes() -> None:
    report = {
        "ok": True,
        "universe": {
            "frontier": [
                {
                    "canonical_name": "Example Labs",
                    "first_seen_at": datetime(2026, 7, 28, 19, 46, tzinfo=UTC),
                    "evidence_date": date(2026, 7, 28),
                }
            ]
        },
    }

    rendered = json.loads(_render_json(report))

    item = rendered["universe"]["frontier"][0]
    assert item["first_seen_at"] == "2026-07-28T19:46:00+00:00"
    assert item["evidence_date"] == "2026-07-28"


def test_health_report_rejects_unknown_python_objects() -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        _render_json({"bad": object()})
