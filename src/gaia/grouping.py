from __future__ import annotations

import hashlib
import re

from .models import Posting

SPACE_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[^a-z0-9+#./ -]+")
LOCATION_SUFFIX_RE = re.compile(
    r"\s+(?:-|–|—|\|)\s+(?:remote|hybrid|onsite|on-site|"
    r"[a-z .'-]+,\s*[a-z]{2}|[a-z .'-]+,\s*(?:usa|united states))$",
    re.I,
)

COMPANY_ALIASES = {
    "google llc": "google",
    "google inc": "google",
    "alphabet": "google",
    "databricks inc": "databricks",
    "cvs health corporation": "cvs health",
}


def normalize_company(company: str) -> str:
    value = SPACE_RE.sub(" ", company.strip().lower())
    value = re.sub(r"[, ]+(inc\.?|llc|ltd\.?|corp\.?|corporation)$", "", value).strip()
    return COMPANY_ALIASES.get(value, value)


def normalize_title(title: str) -> str:
    """Normalize conservatively: remove presentation noise, not specialization."""
    value = title.strip().lower()
    value = LOCATION_SUFFIX_RE.sub("", value)
    value = re.sub(r"\[(?:remote|hybrid|onsite|on-site)\]", "", value, flags=re.I)
    value = re.sub(r"\s*\((?:remote|hybrid|onsite|on-site)\)\s*$", "", value, flags=re.I)
    value = PUNCT_RE.sub(" ", value)
    value = SPACE_RE.sub(" ", value).strip(" -/")
    return value


def family_key(posting: Posting) -> str:
    payload = "|".join(
        [
            normalize_company(posting.company),
            normalize_title(posting.title),
            posting.season or "",
            str(posting.year or ""),
            (posting.employment_type or "").strip().lower(),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def display_title(postings: list[Posting]) -> str:
    # Prefer the shortest non-empty title; location-decorated variants are usually longer.
    titles = sorted({item.title.strip() for item in postings if item.title.strip()}, key=len)
    return titles[0] if titles else "Untitled role"
