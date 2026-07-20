from __future__ import annotations

import re
from dataclasses import replace

from .models import Posting

INTERN_RE = re.compile(r"\b(intern(ship)?|co[- ]?op|industrial placement)\b", re.I)
NEGATIVE_RE = re.compile(
    r"\b(fellow(ship)?|residen(cy|t)|apprentice(ship)?|new grad|graduate program)\b", re.I
)
YEAR_RE = re.compile(r"\b20(?:2[5-9]|3[0-5])\b")
SUMMER_RE = re.compile(r"\bsummer\b", re.I)

CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("quant", re.compile(r"\b(quant|trading|trader|systematic|research analyst)\b", re.I)),
    (
        "ml-ai",
        re.compile(
            r"\b(machine learning|artificial intelligence|\bai\b|deep learning|computer vision|nlp)\b",
            re.I,
        ),
    ),
    (
        "software",
        re.compile(
            r"\b(software|developer|frontend|backend|full[- ]?stack|mobile|ios|android|"
            r"site reliability|sre|devops|platform engineer)\b",
            re.I,
        ),
    ),
    ("data", re.compile(r"\b(data scientist|data engineer|analytics|business intelligence)\b", re.I)),
    ("security", re.compile(r"\b(security|cyber|penetration|vulnerability)\b", re.I)),
    ("hardware", re.compile(r"\b(hardware|silicon|firmware|embedded|electrical|fpga|asic|robotics)\b", re.I)),
    ("product", re.compile(r"\b(product manager|product management|product design|ux|ui)\b", re.I)),
]


def classify(posting: Posting, *, source_confirms_2027: bool = False) -> Posting:
    title = posting.title.strip()
    supporting = " ".join(
        part for part in [posting.employment_type, posting.description[:5000]] if part
    )
    title_is_intern = bool(INTERN_RE.search(title))
    type_is_intern = bool(INTERN_RE.search(posting.employment_type or ""))
    excluded = bool(NEGATIVE_RE.search(title)) and not title_is_intern

    years = {int(value) for value in YEAR_RE.findall(f"{title} {supporting}")}
    year = 2027 if 2027 in years else (next(iter(years)) if len(years) == 1 else None)
    season = "summer" if SUMMER_RE.search(f"{title} {supporting}") else None

    if excluded or not (title_is_intern or type_is_intern):
        target_match = "not_internship"
    elif year == 2027 and season == "summer":
        target_match = "exact"
    elif year == 2027:
        target_match = "year_confirmed"
    elif source_confirms_2027 and season == "summer":
        # A target-specific registry may confirm an omitted year, but it cannot turn a
        # seasonless posting into Summer 2027 merely because the registry contains it.
        target_match = "source_confirmed"
    else:
        target_match = "unknown"

    category = "other"
    classification_text = f"{title} {posting.employment_type}"
    for name, pattern in CATEGORY_RULES:
        if pattern.search(classification_text):
            category = name
            break

    return replace(
        posting,
        category=category,
        season=season,
        year=year,
        target_match=target_match,
    )


def is_default_target(posting: Posting) -> bool:
    return posting.target_match in {"exact", "year_confirmed", "source_confirmed"}
