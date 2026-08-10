from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_url(url: str) -> str:
    """Remove tracking parameters without destroying employer application identity."""
    parts = urlsplit(url.strip())
    blocked = {
        "source",
        "src",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gh_src",
        "lever-source",
        "ref",
        "jr_id",
        "iis",
        "iisn",
    }
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in blocked
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


@dataclass(slots=True)
class Posting:
    company: str
    title: str
    apply_url: str
    source: str
    source_id: str
    locations: list[str] = field(default_factory=list)
    source_mode: str = "direct"
    description: str = ""
    employment_type: str = ""

    # Employer-owned timing. This is only populated when an employer/ATS or an
    # explicitly date-bearing source says when the role was posted.
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    posted_raw: str | None = None
    posted_precision: str = "unknown"
    posted_confidence: str = "unknown"

    # Sensor timing is deliberately separate from employer timing. Community job
    # trackers are excellent low-latency detectors, but their "added"/"age" value
    # must not be silently relabeled as an employer publication timestamp.
    sensor_reported_at: datetime | None = None
    sensor_reported_raw: str | None = None
    sensor_precision: str = "unknown"
    sensor_confidence: str = "unknown"

    # observed_at is GAIA's own fetch time for this exact observation.
    observed_at: datetime = field(default_factory=utcnow)
    category: str = "other"
    season: str | None = None
    year: int | None = None
    target_match: str = "unknown"

    @property
    def posting_key(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def canonical_apply_url(self) -> str:
        return canonical_url(self.apply_url)

    @property
    def market_event_at(self) -> datetime:
        """Best timestamp for ranking discovery urgency, never verification status.

        Employer time wins when available. Otherwise use the source's own listing
        time/age, then GAIA observation time. Verification is an independent axis.
        """
        return self.posted_at or self.sensor_reported_at or self.observed_at


@dataclass(slots=True)
class CollectorResult:
    source: str
    postings: list[Posting]
    complete: bool
    mode: str
    rows_scanned: int
    expected_rows: int | None = None
    error: str | None = None
    status: str = "ok"
    scope: str = "current"
    note: str | None = None
    closed_urls: list[str] = field(default_factory=list)
    discovery_postings: list[Posting] = field(default_factory=list)
