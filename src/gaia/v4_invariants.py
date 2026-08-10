from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import Posting, canonical_url


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _output_openings(families: list[dict[str, object]]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for family in families:
        for raw in family.get("openings") or []:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("apply_url") or "").strip()
            if not url:
                continue
            output[canonical_url(url)].append(raw)
    return output


def validate_sensor_recall(
    sensor_postings: list[Posting],
    families: list[dict[str, object]],
    *,
    now: datetime | None = None,
    minimum_recall: float | None = None,
) -> dict[str, object]:
    """Prove that market sensors cannot silently lose jobs before publication.

    This is the regression invariant for the exact failure that motivated v4:
    another public tracker saw a role, GAIA had ingested the tracker, yet the role
    was absent or buried because the pipeline treated verification/crawl state as
    the inventory boundary.

    Every canonical sensor URL must survive into at least one published opening.
    If a sensor supplied a timestamp, the published opening must retain the newest
    such timestamp even if employer verification later contributes separate timing.
    """
    now = now or datetime.now(UTC)
    if minimum_recall is None:
        minimum_recall = float(os.getenv("GAIA_V4_MIN_SENSOR_RECALL", "0.995"))
    minimum_recall = min(1.0, max(0.0, minimum_recall))

    expected_by_url: dict[str, list[Posting]] = defaultdict(list)
    for posting in sensor_postings:
        if not posting.apply_url:
            continue
        expected_by_url[posting.canonical_apply_url].append(posting)

    actual_by_url = _output_openings(families)
    expected_urls = set(expected_by_url)
    actual_urls = set(actual_by_url)
    missing = sorted(expected_urls - actual_urls)
    recall = 1.0 if not expected_urls else (len(expected_urls) - len(missing)) / len(expected_urls)

    timestamp_drops: list[dict[str, str]] = []
    recent_timestamp_drops: list[dict[str, str]] = []
    recent_cutoff = now - timedelta(hours=24)
    timestamped = 0
    recent_timestamped = 0

    for url, postings in expected_by_url.items():
        expected_times = [posting.sensor_reported_at for posting in postings if posting.sensor_reported_at]
        if not expected_times:
            continue
        timestamped += 1
        expected = max(expected_times)
        if expected.tzinfo is None:
            expected = expected.replace(tzinfo=UTC)
        else:
            expected = expected.astimezone(UTC)
        if expected >= recent_cutoff:
            recent_timestamped += 1

        actual_times = [
            parsed
            for opening in actual_by_url.get(url, [])
            if (parsed := _timestamp(opening.get("sensor_reported_at"))) is not None
        ]
        actual = max(actual_times) if actual_times else None
        if actual is None or actual < expected:
            row = {
                "url": url,
                "expected": expected.isoformat(),
                "actual": actual.isoformat() if actual else "missing",
            }
            timestamp_drops.append(row)
            if expected >= recent_cutoff:
                recent_timestamp_drops.append(row)

    summary: dict[str, object] = {
        "expected_urls": len(expected_urls),
        "published_urls": len(expected_urls & actual_urls),
        "missing_urls": len(missing),
        "recall": round(recall, 6),
        "minimum_recall": minimum_recall,
        "timestamped_urls": timestamped,
        "timestamp_drops": len(timestamp_drops),
        "recent_timestamped_urls": recent_timestamped,
        "recent_timestamp_drops": len(recent_timestamp_drops),
        "missing_sample": missing[:20],
        "timestamp_drop_sample": timestamp_drops[:20],
    }

    if recall < minimum_recall:
        raise RuntimeError(
            f"sensor-to-feed recall {recall:.4%} is below {minimum_recall:.4%}; "
            f"missing={len(missing)}/{len(expected_urls)} sample={missing[:5]}"
        )
    if timestamp_drops:
        raise RuntimeError(
            "sensor timestamp provenance was lost for "
            f"{len(timestamp_drops)} URLs; sample={timestamp_drops[:3]}"
        )
    return summary
