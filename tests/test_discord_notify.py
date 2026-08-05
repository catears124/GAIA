from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gaia.discord_notify import CHANNELS, _payload, _source_label, _webhook_wait_url


def test_verified_alert_uses_one_clean_embed_and_pings_everyone() -> None:
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

    assert payload["content"] == "@everyone"
    assert payload["allowed_mentions"] == {"parse": ["everyone"]}

    embed = payload["embeds"][0]
    assert embed["title"] == "Software Engineer Intern"
    assert embed["url"] == "https://careers.roblox.com/jobs/123"
    assert embed["description"] == "**Roblox**\nSan Mateo, CA"
    assert embed["footer"]["text"] == "GAIA · Verified"
    assert embed["fields"][0] == {
        "name": "Category",
        "value": "Software",
        "inline": True,
    }
    assert embed["fields"][1] == {
        "name": "Source",
        "value": "Employer site · ROBLOX",
        "inline": True,
    }


def test_workday_source_is_presented_without_internal_board_slug() -> None:
    assert (
        _source_label("workday:gdit:external_career_site")
        == "Workday · GDIT"
    )


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
    assert payload["embeds"][0]["footer"]["text"] == "GAIA · Lead"
    assert payload["embeds"][0]["fields"][0]["value"] == "ML / AI"
    assert "Location not stated" in payload["embeds"][0]["description"]


def test_webhook_wait_parameter_is_added_without_losing_existing_query() -> None:
    result = _webhook_wait_url(
        "https://discord.com/api/webhooks/123/token?thread_id=456"
    )

    assert "thread_id=456" in result
    assert "wait=true" in result


def test_non_discord_webhook_is_rejected() -> None:
    with pytest.raises(RuntimeError):
        _webhook_wait_url("https://example.com/api/webhooks/123/token")
