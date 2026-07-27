from __future__ import annotations

import re
from dataclasses import replace

from .models import Posting
from .quality import canonical_company, clean_text, normalize_locations

INTERN_RE = re.compile(
    r"\b(intern(ship)?|co[- ]?op|industrial placement|student placement|summer placement)\b",
    re.I,
)
PROGRAM_RE = re.compile(
    r"\b(summer (?:technology|engineering|software|product|quantitative|trading|research|data) "
    r"(?:analyst|associate|program)|student researcher|university (?:program|role)|campus (?:program|role)|"
    r"technology summer analyst|engineering summer associate)\b",
    re.I,
)
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
    ("quant", re.compile(r"\b(quant(?:itative)?|trading|trader|systematic|research analyst)\b", re.I)),
    (
        "ml-ai",
        re.compile(
            r"\b(machine learning|artificial intelligence|\bai\b|deep learning|computer vision|nlp|"
            r"language model|generative ai)\b",
            re.I,
        ),
    ),
    (
        "software",
        re.compile(
            r"\b(software|developer|frontend|backend|full[- ]?stack|mobile|ios|android|"
            r"site reliability|sre|devops|platform engineer|cloud engineer|technology analyst)\b",
            re.I,
        ),
    ),
    (
        "data",
        re.compile(
            r"\b(data scientist|data engineer|data analyst|analytics|business intelligence|"
            r"database engineer)\b",
            re.I,
        ),
    ),
    ("security", re.compile(r"\b(security|cyber|penetration|vulnerability|threat intelligence)\b", re.I)),
    (
        "hardware",
        re.compile(
            r"\b(hardware|silicon|firmware|embedded|electrical|fpga|asic|robotics|semiconductor)\b",
            re.I,
        ),
    ),
    (
        "product",
        re.compile(r"\b(product manager|product management|program manager|product design|ux|ui)\b", re.I),
    ),
    (
        "other-technical",
        re.compile(
            r"\b(engineer(?:ing)?|technology|technical|computer science|information systems|"
            r"solutions architect|systems analyst)\b",
            re.I,
        ),
    ),
]


def classify(posting: Posting, *, source_confirms_2027: bool = False) -> Posting:
    title = clean_text(posting.title)
    employment_type = clean_text(posting.employment_type)
    employer_description = (
        clean_text(posting.description)
        if posting.source_mode in {"direct", "verification"}
        else ""
    )

    title_is_intern = bool(INTERN_RE.search(title) or PROGRAM_RE.search(title))
    type_is_intern = bool(INTERN_RE.search(employment_type))
    description_is_intern = bool(INTERN_RE.search(employer_description))
    internship_evidence = title_is_intern or type_is_intern or description_is_intern
    excluded = bool(NEGATIVE_RE.search(title)) and not internship_evidence

    title_years = {int(value) for value in YEAR_RE.findall(title)}
    description_years = {int(value) for value in YEAR_RE.findall(employer_description)}
    evidence_years = title_years or description_years
    season_text = f"{title} {employer_description}"
    season = next((name for name, pattern in SEASON_RULES.items() if pattern.search(season_text)), None)
    year = 2027 if 2027 in evidence_years else (
        next(iter(evidence_years)) if len(evidence_years) == 1 else None
    )

    if excluded or not internship_evidence:
        target_match = "not_internship"
    elif (2027 in evidence_years and season == "summer") or (
        SUMMER_2027_RE.search(title) or SUMMER_2027_RE.search(employer_description)
    ):
        target_match = "exact"
    elif evidence_years and 2027 not in evidence_years:
        target_match = "wrong_year"
    elif 2027 in evidence_years and season and season != "summer":
        target_match = "wrong_season"
    elif evidence_years == {2027} and season is None:
        target_match = "year_confirmed"
    elif source_confirms_2027 and not title_years and season == "summer":
        target_match = "source_confirmed"
        year = 2027
    else:
        target_match = "unknown"

    category = "other"
    primary_classification_text = f"{title} {employment_type}"
    for name, pattern in CATEGORY_RULES:
        if pattern.search(primary_classification_text):
            category = name
            break
    if category == "other" and employer_description:
        for name, pattern in CATEGORY_RULES:
            if pattern.search(employer_description):
                category = name
                break

    return replace(
        posting,
        company=canonical_company(posting.company),
        title=title,
        locations=normalize_locations(posting.locations),
        category=category,
        season=season,
        year=year,
        target_match=target_match,
    )


def is_default_target(posting: Posting) -> bool:
    return posting.target_match in {"exact", "year_confirmed", "source_confirmed"}
