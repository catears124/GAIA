from __future__ import annotations

import hashlib
import re

from .models import Posting
from .quality import canonical_company, clean_text, company_key

SPACE_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[^a-z0-9+#./ -]+")
LOCATION_SUFFIX_RE = re.compile(
    r"\s+(?:-|–|—|\|)\s+(?:remote|hybrid|onsite|on-site|"
    r"[a-z .'-]+,\s*[a-z]{2}|[a-z .'-]+,\s*(?:usa|united states))$",
    re.I,
)
SENIORITY_NOISE_RE = re.compile(r"\b(?:i{1,3}|iv|v|1|2|3|4|5)\b$", re.I)


def normalize_company(company: str) -> str:
    return company_key(company)


def normalize_title(title: str) -> str:
    """Normalize conservatively: remove presentation noise, not specialization."""
    value = clean_text(title).casefold()
    value = LOCATION_SUFFIX_RE.sub("", value)
    value = re.sub(r"\[(?:remote|hybrid|onsite|on-site)\]", "", value, flags=re.I)
    value = re.sub(r"\s*\((?:remote|hybrid|onsite|on-site)\)\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*\((?:summer|fall|spring|winter)\s+20\d{2}\)\s*$", "", value, flags=re.I)
    value = re.sub(r"\b(?:summer|fall|spring|winter)\s+20\d{2}\b", "", value, flags=re.I)
    value = PUNCT_RE.sub(" ", value)
    value = SPACE_RE.sub(" ", value).strip(" -/")
    # Collapse noisy level suffixes when they appear after a generic intern title.
    if "intern" in value:
        value = SENIORITY_NOISE_RE.sub("", value).strip()
    return value


def family_key(posting: Posting) -> str:
    payload = "|".join(
        [
            normalize_company(posting.company),
            normalize_title(posting.title),
            posting.season or "",
            str(posting.year or ""),
            (posting.employment_type or "").strip().casefold(),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def display_company(company: str) -> str:
    return canonical_company(company)


def display_title_from_rows(titles: list[str]) -> str:
    cleaned = sorted({clean_text(title) for title in titles if clean_text(title)}, key=lambda item: (len(item), item))
    return cleaned[0] if cleaned else "Untitled role"


def display_title(postings: list[Posting]) -> str:
    return display_title_from_rows([item.title for item in postings])
