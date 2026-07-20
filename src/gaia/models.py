from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    posted_raw: str | None = None
    posted_precision: str = "unknown"
    posted_confidence: str = "unknown"
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
