from __future__ import annotations

import json
import re
from dataclasses import replace
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from .classify import classify
from .models import Posting, canonical_url

INTERN_RE = re.compile(r"\b(?:intern(?:ship)?|co[- ]?op|industrial placement)\b", re.I)
SUMMER_2027_RE = re.compile(r"\b(?:summer\W{0,12}2027|2027\W{0,12}summer)\b", re.I)
CLOSED_RE = re.compile(
    r"\b(?:"
    r"job (?:is )?no longer available|"
    r"position (?:is )?no longer available|"
    r"position has been filled|"
    r"job has expired|"
    r"requisition (?:is )?no longer available|"
    r"no longer accepting applications|"
    r"this (?:job|position) (?:is )?closed|"
    r"the page you requested (?:could not be found|does not exist)"
    r")\b",
    re.I,
)
TITLE_KEYS = ("jobTitle", "job_title", "positionTitle", "position_title", "title", "name")
JOB_SIGNAL_KEYS = {
    "applyUrl",
    "apply_url",
    "description",
    "employmentType",
    "employment_type",
    "jobDescription",
    "jobId",
    "job_id",
    "locations",
    "requisitionId",
    "requisition_id",
}
TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "apply",
    "at",
    "career",
    "careers",
    "co",
    "job",
    "jobs",
    "of",
    "the",
    "to",
    "intern",
    "internship",
    "summer",
    "2027",
}


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unescape(value).casefold()).strip()


def _role_tokens(value: str) -> set[str]:
    return {
        token
        for token in _normalized(value).split()
        if len(token) > 1 and token not in TITLE_STOPWORDS
    }


def _title_similarity(expected: str, candidate: str) -> float:
    expected_tokens = _role_tokens(expected)
    candidate_tokens = _role_tokens(candidate)
    if not expected_tokens or not candidate_tokens:
        return 0.0
    overlap = len(expected_tokens & candidate_tokens)
    return overlap / min(len(expected_tokens), len(candidate_tokens))


def page_is_closed(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    visible = soup.get_text(" ", strip=True)
    return bool(CLOSED_RE.search(visible[:100_000]))


def _json_title_candidates(node: Any, output: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _json_title_candidates(item, output)
        return
    if not isinstance(node, dict):
        return

    has_job_signal = bool(JOB_SIGNAL_KEYS & set(node))
    for key in TITLE_KEYS:
        value = node.get(key)
        if not isinstance(value, str):
            continue
        candidate = re.sub(r"\s+", " ", value).strip()
        if 4 <= len(candidate) <= 240 and (has_job_signal or INTERN_RE.search(candidate)):
            output.append(candidate)

    for value in node.values():
        if isinstance(value, (dict, list)):
            _json_title_candidates(value, output)


def _title_candidates(html: str, soup: BeautifulSoup) -> list[str]:
    output: list[str] = []
    selectors = (
        "h1",
        '[data-automation-id="jobPostingHeader"]',
        '[data-testid="job-title"]',
        '[class*="job-title"]',
        '[class*="jobTitle"]',
    )
    for selector in selectors:
        for node in soup.select(selector):
            candidate = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if 4 <= len(candidate) <= 240:
                output.append(candidate)

    for attribute, value in (
        ("property", "og:title"),
        ("name", "twitter:title"),
        ("name", "title"),
    ):
        node = soup.find("meta", attrs={attribute: value})
        if node and node.get("content"):
            output.append(str(node["content"]).strip())

    if soup.title and soup.title.string:
        output.append(str(soup.title.string).strip())

    for script in soup.select('script[type="application/json"], script#__NEXT_DATA__'):
        body = script.string or script.get_text()
        if not body or len(body) > 5_000_000:
            continue
        try:
            _json_title_candidates(json.loads(body), output)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    for match in re.finditer(
        r'"(?:jobTitle|job_title|positionTitle|position_title)"\s*:\s*"((?:\\.|[^"\\]){4,240})"',
        html,
        re.I,
    ):
        raw = match.group(1)
        try:
            output.append(json.loads(f'"{raw}"'))
        except json.JSONDecodeError:
            output.append(raw)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in output:
        clean = re.sub(r"\s+", " ", unescape(candidate)).strip(" -|\t")
        marker = clean.casefold()
        if clean and marker not in seen:
            seen.add(marker)
            deduped.append(clean)
    return deduped


def _best_title(candidates: list[str], lead: Posting | None) -> tuple[str | None, float]:
    if not candidates:
        return None, 0.0
    if lead is None:
        internship_candidates = [item for item in candidates if INTERN_RE.search(item)]
        if not internship_candidates:
            return None, 0.0
        return min(internship_candidates, key=len), 1.0

    ranked = sorted(
        ((_title_similarity(lead.title, candidate), -len(candidate), candidate) for candidate in candidates),
        reverse=True,
    )
    similarity, _, candidate = ranked[0]
    return candidate, similarity


def posting_from_unstructured_page(
    html: str,
    *,
    page_url: str,
    company: str,
    source: str,
    lead: Posting | None = None,
) -> Posting | None:
    """Recover a posting from a reachable employer page without trusting generic page text.

    A lead-backed page must strongly agree with the indexed role title. A sitemap-only page
    must independently expose an internship title and explicit Summer 2027 evidence.
    """

    if page_is_closed(html):
        return None

    soup = BeautifulSoup(html, "html.parser")
    visible_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    evidence = f"{visible_text} {html[:500_000]}"
    candidates = _title_candidates(html, soup)
    best_title, similarity = _best_title(candidates, lead)

    if lead is not None:
        expected_tokens = _role_tokens(lead.title)
        evidence_tokens = _role_tokens(evidence)
        token_coverage = (
            len(expected_tokens & evidence_tokens) / len(expected_tokens)
            if expected_tokens
            else 0.0
        )
        exact_in_page = _normalized(lead.title) in _normalized(evidence)
        if not INTERN_RE.search(evidence):
            return None
        if not exact_in_page and similarity < 0.55 and token_coverage < 0.75:
            return None
        title = lead.title
        locations = list(lead.locations)
    else:
        if best_title is None or not INTERN_RE.search(best_title):
            return None
        if not SUMMER_2027_RE.search(evidence):
            return None
        title = best_title
        locations = []

    posting = classify(
        Posting(
            company=company,
            title=title,
            apply_url=page_url,
            source=source,
            source_id=canonical_url(page_url),
            locations=locations,
            source_mode="verification",
            description=visible_text[:20_000],
            employment_type="Intern" if INTERN_RE.search(evidence) else "",
        )
    )
    if lead is None and posting.target_match == "unknown" and SUMMER_2027_RE.search(evidence):
        posting = replace(posting, target_match="source_confirmed", year=2027, season="summer")
    return posting
