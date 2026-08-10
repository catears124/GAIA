from __future__ import annotations

import re
from dataclasses import replace

from .models import Posting

SUMMER_2027_RE = re.compile(r"(?:summer\s*[-/]?\s*2027|2027\s*[-/]?\s*summer)", re.I)
OFF_CYCLE_RE = re.compile(r"\b(?:fall|autumn|winter|spring)\b", re.I)
YEAR_2026_RE = re.compile(r"\b2026\b")
YEAR_2027_RE = re.compile(r"\b2027\b")


def _clean_url(url: str) -> str:
    # Raw README regexes can encounter a closing quote immediately after a URL.
    # Never let punctuation become part of the employer application identity.
    return url.strip().rstrip("\"'.,;>")


def normalize_sensor_postings(postings: list[Posting]) -> list[Posting]:
    """Apply cycle truth and URL hygiene after heterogeneous sensor parsing.

    A repository named "Summer 2027" is useful evidence, not permission to relabel
    an explicitly Fall/Winter/Spring role as Summer. Explicit row text always wins.
    A combined role such as "Fall 2026 / Summer 2027" remains valid because Summer
    2027 is explicitly present.
    """
    output: dict[tuple[str, str, str], Posting] = {}
    for posting in postings:
        title = posting.title.strip()
        explicit_summer = bool(SUMMER_2027_RE.search(title))
        if OFF_CYCLE_RE.search(title) and not explicit_summer:
            continue
        if YEAR_2026_RE.search(title) and not YEAR_2027_RE.search(title):
            continue

        cleaned_url = _clean_url(posting.apply_url)
        if not cleaned_url:
            continue
        candidate = posting if cleaned_url == posting.apply_url else replace(posting, apply_url=cleaned_url)
        identity = (candidate.company.casefold(), candidate.title.casefold(), candidate.canonical_apply_url)
        existing = output.get(identity)
        if existing is None or candidate.market_event_at > existing.market_event_at:
            output[identity] = candidate
    return list(output.values())
