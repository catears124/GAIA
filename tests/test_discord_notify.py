from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gaia.discord_notify import (
    CHANNELS,
    VERIFIED_MODES,
    _category_label,
    _payload,
    _source_label,
    _webhook_wait_url,
)


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
    assert embed["title"] == "Roblox"
    assert embed["url"] == "https://careers.roblox.com/jobs/123"
    assert embed["description"] == "**Software Engineer Intern**\nSan Mateo, CA"
    assert embed["footer"]["text"] == "GAIA · Verified"
    assert embed["fields"][0] == {
        "name": "Category",
        "value": "Software",
        "inline": True,
    }
    assert embed["fields"][1] == {
        "name": "Source",
        "value": "Employer site",
        "inline": True,
    }


def test_workday_source_hides_internal_board_slug() -> None:
    assert _source_label("workday:gdit:external_career_site") == "Workday"


def test_generic_category_is_inferred_from_title() -> None:
    assert (
        _category_label("other", "2027 Information Technology Internship")
        == "IT"
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
    assert payload["embeds"][0]["fields"][1]["value"] == "Registry · Simplify"
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


def test_employer_page_promotion_is_a_verified_delivery_event() -> None:
    assert VERIFIED_MODES == ("direct", "verification")
    assert _source_label("verification:example.com:Example") == "Employer page verified"


def test_notifier_reads_committed_postings_without_waiting_for_family_rebuild() -> None:
    source = (Path(__file__).parents[1] / "src" / "gaia" / "discord_notify.py").read_text()
    assert "FROM postings AS posting" in source
    assert "BOOL_OR(posting.source_mode = ANY(%s)) AS has_verified" in source
    assert "discord_notification_claims" in source
    assert 'parser.add_argument("--watch"' in source
    assert 'parser.add_argument("--interval-seconds", type=float, default=2.0)' in source
    assert "FROM families AS family" not in source
