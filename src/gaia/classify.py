from __future__ import annotations

import re
from dataclasses import replace

from .models import Posting

INTERN_RE = re.compile(r"\b(intern(ship)?|co[- ]?op|industrial placement)\b", re.I)
NEGATIVE_RE = re.compile(
    r"\b(fellow(ship)?|residen(cy|t)|apprentice(ship)?|new grad|graduate program)\b", re.I
)
YEAR_RE = re.compile(r"\b20(?:2[5-9]|3[0-5])\b")
SUMMER_2027_RE = re.compile(r"\b(?:summer\W{0,12}2027|2027\W{0,12}summer)\b", re.I)
SEASON_RULES = {
    "summer": re.compile(r"\bsummer\b", re.I),
    "fall": re.compile(r"\b(fall|autumn)\b", re.I),
    "spring": re.compile(r"\bspring\b", re.I),
    "winter": re.compile(r"\bwinter\b", re.I),
}

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
    title_is_intern = bool(INTERN_RE.search(title))
    type_is_intern = bool(INTERN_RE.search(posting.employment_type or ""))
    excluded = bool(NEGATIVE_RE.search(title)) and not title_is_intern

    title_years = {int(value) for value in YEAR_RE.findall(title)}
    season = next((name for name, pattern in SEASON_RULES.items() if pattern.search(title)), None)
    year = 2027 if 2027 in title_years else (
        next(iter(title_years)) if len(title_years) == 1 else None
    )

    if excluded or not (title_is_intern or type_is_intern):
        target_match = "not_internship"
    elif SUMMER_2027_RE.search(title):
        target_match = "exact"
    elif title_years and 2027 not in title_years:
        target_match = "wrong_year"
    elif 2027 in title_years and season and season != "summer":
        target_match = "wrong_season"
    elif title_years == {2027} and season is None:
        target_match = "year_confirmed"
    elif source_confirms_2027 and not title_years and season == "summer":
        # Registry provenance can fill an omitted year, never override a stated one.
        target_match = "source_confirmed"
        year = 2027
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
