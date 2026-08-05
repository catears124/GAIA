from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .db import Database
from .quality import TECH_CATEGORIES

TARGET_MATCHES = ("exact", "year_confirmed", "source_confirmed")
LEAD_MODES = ("registry", "external-index", "verification-lead")

_CATEGORY_LABELS = {
    "software": "Software",
    "ml-ai": "ML / AI",
    "data": "Data",
    "security": "Security",
    "hardware": "Hardware",
    "quant": "Quant",
    "product": "Product",
    "other-technical": "Other technical",
}

_SOURCE_LABELS = {
    "ashby": "Ashby",
    "direct": "Employer site",
    "domain": "Employer site",
    "external-index": "External index",
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "registry": "Registry",
    "rippling": "Rippling",
    "smartrecruiters": "SmartRecruiters",
    "teamtailor": "Teamtailor",
    "verification-lead": "Verification lead",
    "workday": "Workday",
}


@dataclass(frozen=True, slots=True)
class Channel:
    name: str
    secret: str
    label: str
    color: int


CHANNELS = (
    Channel("verified", "VERIFIED_DHOOK", "Verified", 0x57F287),
    Channel("leads", "LEADS_DHOOK", "Lead", 0xFEE75C),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_notification_channels (
    channel TEXT PRIMARY KEY,
    initialized_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS discord_notification_deliveries (
    channel TEXT NOT NULL REFERENCES discord_notification_channels(channel) ON DELETE CASCADE,
    family_key TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('sent', 'suppressed')),
    discord_message_id TEXT,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, family_key)
);

CREATE INDEX IF NOT EXISTS idx_discord_notification_deliveries_time
    ON discord_notification_deliveries (delivered_at DESC);
"""


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from error
    return max(minimum, min(value, maximum))


def _truncate(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _webhook_wait_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or parts.netloc not in {"discord.com", "discordapp.com"}:
        raise RuntimeError("Discord webhook must use an official Discord HTTPS webhook URL")
    if not parts.path.startswith("/api/webhooks/"):
        raise RuntimeError("Discord webhook URL does not contain /api/webhooks/")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["wait"] = "true"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _iso(value: object) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _locations(row: dict[str, Any]) -> str:
    values = row.get("locations") or []
    if isinstance(values, str):
        values = [values]
    cleaned = [str(value).strip() for value in values if str(value).strip()]
    return ", ".join(cleaned) or "Location not stated"


def _category_label(value: object) -> str:
    key = _truncate(value, 80).lower()
    if not key:
        return "Other"
    return _CATEGORY_LABELS.get(key, key.replace("-", " ").title())


def _source_token(value: str) -> str:
    cleaned = value.replace("_", " ").replace("-", " ").strip()
    if not cleaned:
        return ""
    if cleaned.isalpha() and len(cleaned) <= 5:
        return cleaned.upper()
    return cleaned.title()


def _source_label(value: object) -> str:
    raw = _truncate(value, 180) or "unknown"
    parts = [part.strip() for part in raw.split(":")]
    kind = parts[0].lower()
    provider = _SOURCE_LABELS.get(kind)
    if provider is None:
        return raw

    detail = ""
    if kind == "domain" and len(parts) >= 3:
        detail = parts[-1]
    elif len(parts) >= 2:
        detail = _source_token(parts[1])

    return _truncate(f"{provider} · {detail}" if detail else provider, 180)


def _relative_timestamp(value: str) -> str:
    epoch = int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    return f"<t:{epoch}:R>"


def _payload(row: dict[str, Any], channel: Channel) -> dict[str, Any]:
    company = _truncate(row.get("company"), 120) or "Unknown company"
    title = _truncate(row.get("title"), 180) or "Untitled role"
    location = _truncate(_locations(row), 500)
    apply_url = str(row.get("apply_url") or "")
    source = _source_label(row.get("source"))
    category = _category_label(row.get("category"))
    posted = _iso(row.get("posted_at"))
    detected = _iso(row.get("first_detected_at"))

    fields: list[dict[str, object]] = [
        {"name": "Category", "value": category, "inline": True},
        {"name": "Source", "value": source, "inline": True},
    ]
    if posted:
        fields.append(
            {
                "name": "Employer posted",
                "value": _relative_timestamp(posted),
                "inline": True,
            }
        )
    elif detected:
        fields.append(
            {
                "name": "Found",
                "value": _relative_timestamp(detected),
                "inline": True,
            }
        )

    embed: dict[str, object] = {
        "title": _truncate(title, 256),
        "url": apply_url,
        "description": _truncate(f"**{company}**\n{location}", 4_096),
        "color": channel.color,
        "fields": fields,
        "footer": {"text": f"GAIA · {channel.label}"},
    }
    if detected:
        embed["timestamp"] = detected
    return {
        "username": f"GAIA {channel.label}",
        "content": "@everyone",
        "embeds": [embed],
        "allowed_mentions": {"parse": ["everyone"]},
    }


def _post_with_retry(
    client: httpx.Client,
    webhook_url: str,
    payload: dict[str, Any],
    *,
    attempts: int = 6,
) -> str | None:
    url = _webhook_wait_url(webhook_url)
    for attempt in range(1, attempts + 1):
        response = client.post(url, json=payload)
        if response.status_code in {200, 204}:
            if not response.content:
                return None
            try:
                body = response.json()
            except ValueError:
                return None
            return str(body.get("id") or "") or None
        if response.status_code == 429:
            try:
                retry_after = float(response.json().get("retry_after", 1.0))
            except (ValueError, TypeError, AttributeError):
                retry_after = 1.0
            time.sleep(max(0.25, min(retry_after, 30.0)))
            continue
        if response.status_code >= 500 and attempt < attempts:
            time.sleep(min(2 ** (attempt - 1), 20))
            continue
        raise RuntimeError(
            f"Discord webhook failed with HTTP {response.status_code}: "
            f"{_truncate(response.text, 500)}"
        )
    raise RuntimeError("Discord webhook exhausted retries")


def _channel_filter(channel: Channel) -> tuple[str, list[object]]:
    if channel.name == "verified":
        return "family.direct_openings > 0 AND posting.source_mode='direct'", []
    return (
        "family.direct_openings = 0 AND family.backstop_openings > 0 "
        "AND posting.source_mode = ANY(%s)",
        [list(LEAD_MODES)],
    )


def _ensure_channel(connection: Any, channel: Channel, lookback_minutes: int) -> int:
    inserted = connection.execute(
        """
        INSERT INTO discord_notification_channels(channel)
        VALUES (%s)
        ON CONFLICT(channel) DO NOTHING
        RETURNING channel
        """,
        (channel.name,),
    ).fetchone()
    if inserted is None:
        return 0

    predicate, predicate_params = _channel_filter(channel)
    result = connection.execute(
        f"""
        INSERT INTO discord_notification_deliveries(channel, family_key, disposition)
        SELECT %s, family.family_key, 'suppressed'
        FROM families AS family
        JOIN postings AS posting ON posting.family_key=family.family_key
        WHERE posting.active
          AND posting.removed_at IS NULL
          AND {predicate}
          AND family.target_match = ANY(%s)
          AND family.category = ANY(%s)
          AND family.first_detected_at < now() - make_interval(mins => %s)
        GROUP BY family.family_key
        ON CONFLICT(channel, family_key) DO NOTHING
        """,
        [
            channel.name,
            *predicate_params,
            list(TARGET_MATCHES),
            list(TECH_CATEGORIES),
            lookback_minutes,
        ],
    )
    return int(result.rowcount or 0)


def _pending(connection: Any, channel: Channel, limit: int) -> list[dict[str, Any]]:
    predicate, predicate_params = _channel_filter(channel)
    rows = connection.execute(
        f"""
        SELECT DISTINCT ON (family.family_key)
            family.family_key,
            family.company,
            family.title,
            family.locations,
            family.category,
            family.first_detected_at,
            posting.apply_url,
            posting.source,
            posting.posted_at,
            posting.first_seen_at
        FROM families AS family
        JOIN postings AS posting ON posting.family_key=family.family_key
        LEFT JOIN discord_notification_deliveries AS delivered
          ON delivered.channel=%s AND delivered.family_key=family.family_key
        WHERE delivered.family_key IS NULL
          AND posting.active
          AND posting.removed_at IS NULL
          AND {predicate}
          AND family.target_match = ANY(%s)
          AND family.category = ANY(%s)
        ORDER BY family.family_key,
                 posting.posted_at DESC NULLS LAST,
                 posting.first_seen_at DESC,
                 posting.posting_key
        LIMIT %s
        """,
        [
            channel.name,
            *predicate_params,
            list(TARGET_MATCHES),
            list(TECH_CATEGORIES),
            limit,
        ],
    ).fetchall()
    return [dict(row) for row in rows]


def _mark_sent(
    connection: Any,
    channel: Channel,
    family_key: str,
    message_id: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO discord_notification_deliveries(
            channel, family_key, disposition, discord_message_id
        )
        VALUES (%s, %s, 'sent', %s)
        ON CONFLICT(channel, family_key) DO NOTHING
        """,
        (channel.name, family_key, message_id),
    )
    connection.execute(
        "UPDATE discord_notification_channels SET updated_at=now() WHERE channel=%s",
        (channel.name,),
    )


def send_notifications(database: Database | None = None) -> dict[str, object]:
    database = database or Database(migrate=False)
    lookback = _bounded_int(
        "GAIA_DISCORD_BOOTSTRAP_LOOKBACK_MINUTES",
        180,
        minimum=1,
        maximum=1440,
    )
    limit = _bounded_int(
        "GAIA_DISCORD_MAX_PER_CHANNEL",
        100,
        minimum=1,
        maximum=500,
    )
    summary: dict[str, object] = {"lookback_minutes": lookback, "channels": {}}

    with database.connect() as connection:
        connection.execute(_SCHEMA)

    with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        for channel in CHANNELS:
            webhook = os.getenv(channel.secret, "").strip()
            channel_result: dict[str, object] = {
                "configured": bool(webhook),
                "suppressed_on_bootstrap": 0,
                "pending": 0,
                "sent": 0,
            }
            summary["channels"][channel.name] = channel_result  # type: ignore[index]
            if not webhook:
                continue

            with database.connect() as connection:
                channel_result["suppressed_on_bootstrap"] = _ensure_channel(
                    connection, channel, lookback
                )
                pending = _pending(connection, channel, limit)
            channel_result["pending"] = len(pending)

            for row in pending:
                message_id = _post_with_retry(client, webhook, _payload(row, channel))
                with database.connect() as connection:
                    _mark_sent(connection, channel, str(row["family_key"]), message_id)
                channel_result["sent"] = int(channel_result["sent"]) + 1

    return summary


def main() -> int:
    try:
        result = send_notifications()
    except Exception as error:  # noqa: BLE001 - CLI must surface delivery failures to Actions.
        print(f"Discord notification failure: {error!r}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
