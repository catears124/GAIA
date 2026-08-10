from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from .models import Posting

TECH_CATEGORIES = {
    "software",
    "ml-ai",
    "quant",
    "security",
    "data",
    "product",
    "hardware",
    "other-technical",
}


def _clean_url(url: str) -> str:
    # Raw README regexes can encounter a closing quote immediately after a URL.
    # Never let punctuation become part of the employer application identity.
    return url.strip().rstrip("\"'.,;>")


def is_current_market_target(posting: Posting, *, now: datetime | None = None) -> bool:
    """Return whether an active sensor row belongs in GAIA's technical market.

    Summer 2027 is a *view/filter*, not the ingestion boundary. A live Fall 2026 or
    Spring 2027 SWE internship is still a current internship and should appear in
    the default market feed with its real cycle. This is the distinction the old
    pipeline lost when it discarded everything outside the target season before
    ranking freshness.
    """
    if posting.target_match == "not_internship":
        return False
    if posting.category not in TECH_CATEGORIES:
        return False
    current_year = (now or datetime.now(UTC)).year
    if posting.year is not None and posting.year < current_year:
        return False
    return True


def normalize_sensor_postings(postings: list[Posting]) -> list[Posting]:
    """Clean URL identity and de-duplicate heterogeneous market observations."""
    output: dict[tuple[str, str, str], Posting] = {}
    for posting in postings:
        cleaned_url = _clean_url(posting.apply_url)
        if not cleaned_url:
            continue
        candidate = posting if cleaned_url == posting.apply_url else replace(posting, apply_url=cleaned_url)
        identity = (
            candidate.company.casefold(),
            candidate.title.casefold(),
            candidate.canonical_apply_url,
        )
        existing = output.get(identity)
        if existing is None or candidate.market_event_at > existing.market_event_at:
            output[identity] = candidate
    return list(output.values())
