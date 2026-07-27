from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

SPACE_RE = re.compile(r"\s+")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MARKDOWN_WRAPPER_RE = re.compile(r"^\[(.+)\]\(https?://[^)]+\)$", re.I)
HTML_TAG_RE = re.compile(r"<\s*br\s*/?\s*>|</p>|</div>|</li>", re.I)
TAG_RE = re.compile(r"<[^>]+>")
FLAG_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002B00-\U00002BFF"
    "]+",
    re.UNICODE,
)
LOCATION_COUNT_RE = re.compile(r"^\s*\d+\s+locations?\s*\*+\s*", re.I)
STATE_RE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY)(?=$|[\s,;|·—–]|[A-Z][a-z])"
)
COUNTRY_HINTS = {
    "united states",
    "usa",
    "us",
    "canada",
    "uk",
    "united kingdom",
    "ireland",
    "australia",
    "singapore",
    "india",
}
EMPTY_LOCATION_VALUES = {"", "—", "-", "–", "not stated", "location not stated", "n/a"}
NON_LOCATION_VALUES = {"(multiple us)", "multiple u.s. locations", "multiple us locations"}
ISO_DATE_LOCATION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TECH_CATEGORIES = ("software", "ml-ai", "data", "security", "hardware", "quant", "product")
NON_APPLICATION_HOSTS = {
    "github.com",
    "www.github.com",
    "simplify.jobs",
    "www.simplify.jobs",
    "speedyapply.com",
    "www.speedyapply.com",
    "discord.gg",
    "www.linkedin.com",
    "jobright.ai",
    "www.jobright.ai",
    "workopia.io",
    "www.workopia.io",
    "visasponsor.jobs",
    "www.visasponsor.jobs",
}


def is_actionable_application_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and parts.netloc.lower() not in NON_APPLICATION_HOSTS


def is_specific_application_url(url: str) -> bool:
    """Reject employer home/search pages that cannot open the advertised requisition."""
    if not is_actionable_application_url(url):
        return False
    parts = urlsplit(url)
    if re.search(r"(?:^|&)gh_jid=\d+(?:&|$)", parts.query, re.I):
        return True
    path = parts.path.rstrip("/").casefold()
    if path in {
        "",
        "/careers",
        "/jobs",
        "/open-positions",
        "/about/careers/applications/jobs/results",
    }:
        return False
    return not path.endswith(("/careers", "/jobs", "/search", "/jobs/results"))


COMPANY_ALIASES = {
    "alphabet": "Google",
    "google": "Google",
    "google llc": "Google",
    "google inc": "Google",
    "databricks": "Databricks",
    "databricks inc": "Databricks",
    "de shaw": "D. E. Shaw",
    "d e shaw": "D. E. Shaw",
    "d. e. shaw": "D. E. Shaw",
    "d.e. shaw": "D. E. Shaw",
    "the d. e. shaw group": "D. E. Shaw",
    "the d e shaw group": "D. E. Shaw",
    "bae systems": "BAE Systems",
    "bae systems inc": "BAE Systems",
    "northrop grumman": "Northrop Grumman",
    "northrop grumman corporation": "Northrop Grumman",
    "susquehanna international group sig": "Susquehanna International Group",
    "susquehanna international group": "Susquehanna International Group",
    "sig": "Susquehanna International Group",
    "hudson river trading": "Hudson River Trading",
    "hrt": "Hudson River Trading",
    "tiktok": "TikTok",
    "byte dance": "ByteDance",
    "bytedance": "ByteDance",
    "cvs health": "CVS Health",
    "cvs health corporation": "CVS Health",
}
ORG_SUFFIX_RE = re.compile(
    r"\b(incorporated|inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|sa|ag)\.?$",
    re.I,
)


def clean_text(value: object) -> str:
    text = str(value or "")
    text = HTML_TAG_RE.sub(" | ", text)
    text = MARKDOWN_WRAPPER_RE.sub(r"\1", text)
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    text = TAG_RE.sub(" ", text)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    text = text.replace("\u00a0", " ").replace("\ufe0f", "")
    text = FLAG_RE.sub("", text)
    text = EMOJI_RE.sub("", text)
    return SPACE_RE.sub(" ", text).strip(" *\t|·-—–")


def _company_key(company: str) -> str:
    value = clean_text(company).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = SPACE_RE.sub(" ", value).strip()
    value = ORG_SUFFIX_RE.sub("", value).strip()
    return value


def canonical_company(company: str) -> str:
    key = _company_key(company)
    return COMPANY_ALIASES.get(key, clean_text(company).strip() or "Unknown company")


def company_key(company: str) -> str:
    return _company_key(canonical_company(company))


def _split_joined_us_locations(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for match in STATE_RE.finditer(value):
        end = match.end()
        fragment = value[start:end].strip(" ,;|·—–")
        if fragment:
            parts.append(fragment)
        start = end
    tail = value[start:].strip(" ,;|·—–")
    if tail and not parts:
        parts.append(tail)
    elif tail and len(tail.split()) <= 5 and any(hint in tail.casefold() for hint in COUNTRY_HINTS):
        parts.append(tail)
    return parts or [value]


def normalize_locations(values: object) -> list[str]:
    if values is None:
        return []
    raw_values = values if isinstance(values, list) else [values]
    output: list[str] = []
    for raw in raw_values:
        value = clean_text(raw)
        if value.casefold() in EMPTY_LOCATION_VALUES or ISO_DATE_LOCATION_RE.fullmatch(value):
            continue
        if value.casefold() in NON_LOCATION_VALUES:
            value = "United States"
        value = LOCATION_COUNT_RE.sub("", value)
        value = re.sub(r"\b\d+\s+locations?\b", " ", value, flags=re.I)
        value = value.replace("Remote-Friendly", "Remote")
        chunks = re.split(r"\s*(?:\||/|;|·|\n|\r|\t|\*\*)\s*", value)
        for chunk in chunks:
            chunk = SPACE_RE.sub(" ", chunk).strip(" ,;-·*—–")
            if chunk.casefold() in EMPTY_LOCATION_VALUES:
                continue
            for fixed in _split_joined_us_locations(chunk):
                fixed = SPACE_RE.sub(" ", fixed).strip(" ,;-·*—–")
                if fixed.casefold() not in EMPTY_LOCATION_VALUES:
                    output.append(fixed)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in output:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def canonical_source_name(source: str) -> str:
    source = clean_text(source)
    if source.startswith("ashby:"):
        return "ashby:" + source.split(":", 1)[1].casefold()
    if source.startswith("greenhouse:"):
        return "greenhouse:" + source.split(":", 1)[1].casefold()
    if source.startswith("lever:"):
        return "lever:" + source.split(":", 1)[1].casefold()
    if source.startswith("workday:"):
        parts = source.split(":")
        if len(parts) >= 3:
            return ":".join([parts[0], parts[1].casefold(), parts[2].casefold()])
    return source


def canonical_application_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def is_independent_mode(mode: str) -> bool:
    return mode in {"direct", "verification"}


def is_index_mode(mode: str) -> bool:
    return mode in {"registry", "external-index", "verification-lead"}
