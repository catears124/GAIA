from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gaia.discord_notify import CHANNELS, _payload, _webhook_wait_url


def test_verified_alert_pings_everyone_with_company_first() -> None:
    channel = next(item for item in CHANNELS if item.name == "verified")
    payload = _payload(
        {
            "company": "Roblox",
            "title": "Software Engineer Intern",
            "locations": ["San Mateo, CA"],
            "apply_url": "https://careers.roblox.com/jobs/123",
            "source": "direct:roblox",
            "category": "software",
            "first_detected_at": datetime(2026, 8, 5, 18, tzinfo=UTC),
        },
        channel,
    )

    lines = payload["content"].splitlines()
    assert lines[0] == "# **Roblox**"
    assert lines[1] == "@everyone"
    assert lines[2] == "## Software Engineer Intern"
    assert payload["allowed_mentions"] == {"parse": ["everyone"]}
    assert payload["embeds"][0]["footer"]["text"] == "GAIA • Verified"


def test_lead_alert_is_visibly_distinct() -> None:
    channel = next(item for item in CHANNELS if item.name == "leads")
    payload = _payload(
        {
            "company": "Example",
            "title": "ML Intern",
            "locations": [],
            "apply_url": "https://example.com/jobs/1",
            "source": "registry:simplify",
            "category": "ml-ai",
        },
        channel,
    )

    assert payload["username"] == "GAIA Lead"
    assert payload["embeds"][0]["fields"][0]["value"] == "Lead"
    assert "Location not stated" in payload["content"]


def test_webhook_wait_parameter_is_added_without_losing_existing_query() -> None:
    result = _webhook_wait_url(
        "https://discord.com/api/webhooks/123/token?thread_id=456"
    )

    assert "thread_id=456" in result
    assert "wait=true" in result


def test_non_discord_webhook_is_rejected() -> None:
    with pytest.raises(RuntimeError):
        _webhook_wait_url("https://example.com/api/webhooks/123/token")
