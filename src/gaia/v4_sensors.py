from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from dateutil import parser as date_parser

from .classify import classify
from .models import Posting, canonical_url

DEFAULT_CONFIG = Path(__file__).with_name("v4_sources.yaml")

LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)[^)]*\)")
HTML_LINK_RE = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)
RAW_URL_RE = re.compile(r"https?://[^\s<>|)\]]+")
SEPARATOR_RE = re.compile(r"^:?-{2,}:?$")
RELATIVE_RE = re.compile(
    r"(?<!\d)(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)(?:\s+ago)?\b",
    re.I,
)

COMPANY_HEADERS = {"company", "employer", "firm", "organization", "organisation"}
TITLE_HEADERS = {"role", "position", "title", "job", "internship", "job title", "position title"}
LOCATION_HEADERS = {"location", "locations", "office", "city"}
TIME_HEADERS = {
    "age",
    "added",
    "date",
    "date added",
    "date posted",
    "posted",
    "posted date",
    "posting date",
    "published",
    "published at",
}
NUFT_ROLE_NAMES = {
    "swe": "Software Engineering Intern",
    "qt": "Quantitative Trading Intern",
    "qd": "Quantitative Developer Intern",
    "qr": "Quantitative Research Intern",
    "phd": "PhD Quantitative Research Intern",
    "hw": "Hardware Engineering Intern",
    "fpga": "FPGA Engineering Intern",
}


@dataclass(frozen=True, slots=True)
class SensorSpec:
    name: str
    kind: str
    url: str
    cycle: str = "mixed"
    priority: int = 0

    @property
    def source(self) -> str:
        return f"sensor:{self.name}"


@dataclass
class SensorRun:
    name: str
    url: str
    status: str
    rows: int
    postings: int
    fetched_at: str
    error: str | None = None


def load_sensor_specs(path: Path = DEFAULT_CONFIG) -> list[SensorSpec]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    specs = [
        SensorSpec(
            name=str(item["name"]),
            kind=str(item["kind"]),
            url=str(item["url"]),
            cycle=str(item.get("cycle") or "mixed"),
            priority=int(item.get("priority") or 0),
        )
        for item in raw.get("sensors", [])
    ]
    return sorted(specs, key=lambda item: (-item.priority, item.name))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_sensor_time(raw: object, now: datetime) -> tuple[datetime | None, str]:
    value = str(raw or "").strip()
    if not value or value.casefold() in {"-", "—", "n/a", "na", "unknown", "none"}:
        return None, "unknown"
    lowered = value.casefold()
    if lowered in {"today", "new", "just now"}:
        return now, "day"
    if lowered == "yesterday":
        return now - timedelta(days=1), "day"
    match = RELATIVE_RE.search(value)
    if match:
        count = int(match.group(1))
        unit = match.group(2).casefold()
        if unit.startswith("m"):
            return now - timedelta(minutes=count), "relative"
        if unit.startswith("h"):
            return now - timedelta(hours=count), "relative"
        if unit.startswith("d"):
            return now - timedelta(days=count), "day"
        if unit.startswith("w"):
            return now - timedelta(weeks=count), "day"
    try:
        parsed = _aware(date_parser.parse(value, fuzzy=False))
    except (TypeError, ValueError, OverflowError):
        return None, "unknown"
    precision = "timestamp" if re.search(r"\d:\d|T\d", value) else "date"
    return parsed, precision


def _clean_text(value: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", value)
    value = LINK_RE.sub(lambda match: match.group(1), value)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value.replace("&nbsp;", " ")).strip(" *\t")


def _header(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", _clean_text(value).casefold())
    return re.sub(r"\s+", " ", value).strip()


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    return [cell.strip() for cell in stripped.strip("|").split("|")] if stripped.startswith("|") else []


def _separator(cells: list[str]) -> bool:
    return bool(cells) and all(SEPARATOR_RE.match(cell.replace(" ", "")) for cell in cells)


def _links(line: str) -> list[str]:
    values = [match.group(2) for match in LINK_RE.finditer(line)]
    values.extend(HTML_LINK_RE.findall(line))
    values.extend(RAW_URL_RE.findall(line))
    return list(dict.fromkeys(url.rstrip(".,") for url in values))


def _url_score(url: str) -> int:
    try:
        parts = urlsplit(url)
    except ValueError:
        return -10_000
    host = parts.netloc.casefold()
    path = parts.path.casefold()
    if parts.scheme not in {"http", "https"} or not host:
        return -10_000
    if any(fragment in url.casefold() for fragment in ("github.com", "discord.gg", "linkedin.com/company")):
        return -1_000
    score = 0
    if any(
        fragment in host
        for fragment in (
            "greenhouse.io",
            "lever.co",
            "ashbyhq.com",
            "myworkdayjobs.com",
            "smartrecruiters.com",
            "oraclecloud.com",
            "icims.com",
            "jobvite.com",
            "workable.com",
            "recruitee.com",
            "rippling.com",
            "amazon.jobs",
        )
    ):
        score += 600
    if host.startswith(("jobs.", "careers.")):
        score += 100
    if any(marker in path for marker in ("/job/", "/jobs/", "/position/", "/apply", "/career")):
        score += 250
    if "job" in host or "career" in host:
        score += 80
    if path in {"", "/"}:
        score -= 200
    return score


def _choose_url(urls: list[str]) -> str | None:
    ranked = [(score, index, value) for index, value in enumerate(urls) if (score := _url_score(value)) > -1_000]
    return max(ranked, default=None, key=lambda item: (item[0], item[1]))[2] if ranked else None


def _index(headers: list[str], aliases: set[str]) -> int | None:
    return next((index for index, name in enumerate(headers) if name in aliases), None)


def _markdown_rows(body: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    headers: list[str] = []
    current_company = ""
    previous_company = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            current_company = _clean_text(stripped[3:])
            headers = []
            continue
        cells = _cells(line)
        if not cells or _separator(cells):
            continue
        normalized = [_header(cell) for cell in cells]
        title_i = _index(normalized, TITLE_HEADERS)
        company_i = _index(normalized, COMPANY_HEADERS)
        if title_i is not None and (company_i is not None or current_company):
            headers = normalized
            continue
        if not headers:
            continue
        title_i = _index(headers, TITLE_HEADERS)
        if title_i is None or title_i >= len(cells):
            continue
        company_i = _index(headers, COMPANY_HEADERS)
        location_i = _index(headers, LOCATION_HEADERS)
        time_i = _index(headers, TIME_HEADERS)
        company = _clean_text(cells[company_i]) if company_i is not None and company_i < len(cells) else current_company
        if company in {"", "↳", "—", "-", "⬆"}:
            company = previous_company or current_company
        elif company_i is not None:
            previous_company = company
        title = _clean_text(cells[title_i])
        if company_i is None and current_company:
            title = NUFT_ROLE_NAMES.get(title.casefold(), title)
        if not company or not title:
            continue
        location = _clean_text(cells[location_i]) if location_i is not None and location_i < len(cells) else ""
        timing = _clean_text(cells[time_i]) if time_i is not None and time_i < len(cells) else ""
        url = _choose_url(_links(line))
        if not url or "🔒" in line:
            continue
        rows.append({"company": company, "title": title, "location": location, "timing": timing, "url": url})
    return rows


def _json_rows(payload: Any, spec: SensorSpec) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if spec.kind == "json-map":
        return [item for item in payload.values() if isinstance(item, dict)]
    for key in ("jobs", "listings", "items", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _value(item: dict[str, Any], *names: str) -> Any:
    return next((item[name] for name in names if name in item and item[name] not in (None, "")), None)


def _locations(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(value).strip() for value in raw if str(value).strip()]
    if isinstance(raw, dict):
        return [str(value).strip() for value in raw.values() if str(value).strip()]
    value = str(raw or "").strip()
    return [value] if value else []


def _mixed_cycle_is_target(item: dict[str, Any]) -> bool:
    if _value(item, "is_open", "active") is False:
        return False
    season = str(_value(item, "season", "cycle") or "").casefold()
    if "summer 2027" in season:
        return True
    title = str(_value(item, "title", "role", "position") or "").casefold()
    return "2027" in title and "intern" in title and not any(term in title for term in ("fall", "winter", "spring"))


def parse_sensor(spec: SensorSpec, body: str, fetched_at: datetime) -> tuple[list[Posting], int]:
    rows: list[dict[str, Any]]
    if spec.kind == "markdown":
        rows = _markdown_rows(body)
    else:
        rows = _json_rows(json.loads(body), spec)

    postings: list[Posting] = []
    for index, item in enumerate(rows):
        if spec.cycle == "mixed" and not _mixed_cycle_is_target(item):
            continue
        if _value(item, "is_visible") is False or _value(item, "is_open", "active") is False:
            continue
        company = str(_value(item, "company", "company_name", "employer") or "").strip()
        title = str(_value(item, "title", "role", "position", "job_title") or "").strip()
        url = str(_value(item, "url", "apply_url", "job_url", "application_url") or "").strip()
        if not company or not title or not url:
            continue
        raw_time = _value(item, "timing", "date_posted", "posted_at", "first_seen_at", "date_added", "added", "age")
        sensor_at, precision = parse_sensor_time(raw_time, fetched_at)
        posting = Posting(
            company=company,
            title=title,
            apply_url=url,
            source=spec.source,
            source_id=str(_value(item, "id", "job_id", "source_id") or canonical_url(url) or index),
            locations=_locations(_value(item, "location", "locations", "locationsText")),
            source_mode="market-sensor",
            sensor_reported_at=sensor_at,
            sensor_reported_raw=str(raw_time) if raw_time not in (None, "") else None,
            sensor_precision=precision,
            sensor_confidence="source-reported" if sensor_at else "unknown",
            observed_at=fetched_at,
        )
        classified = classify(posting, source_confirms_2027=spec.cycle == "summer-2027")
        if spec.cycle == "summer-2027" and classified.target_match == "unknown":
            classified = replace(classified, year=2027, season="summer", target_match="source_confirmed")
        elif spec.cycle == "mixed" and "summer 2027" in str(_value(item, "season", "cycle") or "").casefold():
            if classified.target_match == "unknown":
                classified = replace(classified, year=2027, season="summer", target_match="source_confirmed")
        postings.append(classified)

    by_identity: dict[tuple[str, str, str], Posting] = {}
    for posting in postings:
        identity = (posting.company.casefold(), posting.title.casefold(), posting.canonical_apply_url)
        existing = by_identity.get(identity)
        if existing is None or posting.market_event_at > existing.market_event_at:
            by_identity[identity] = posting
    return list(by_identity.values()), len(rows)


async def fetch_sensor(
    client: httpx.AsyncClient,
    spec: SensorSpec,
    *,
    semaphore: asyncio.Semaphore,
) -> tuple[list[Posting], SensorRun]:
    fetched_at = datetime.now(UTC)
    async with semaphore:
        try:
            response = await client.get(spec.url)
            response.raise_for_status()
            postings, rows = parse_sensor(spec, response.text, fetched_at)
            return postings, SensorRun(spec.name, spec.url, "ok", rows, len(postings), fetched_at.isoformat())
        except Exception as exc:  # noqa: BLE001 - isolate one public sensor
            return [], SensorRun(spec.name, spec.url, "failed", 0, 0, fetched_at.isoformat(), repr(exc)[:500])


async def fetch_all_sensors(
    specs: list[SensorSpec] | None = None,
    *,
    concurrency: int = 8,
    timeout_seconds: float = 30.0,
) -> tuple[list[Posting], list[SensorRun]]:
    specs = specs or load_sensor_specs()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    limits = httpx.Limits(max_connections=max(16, concurrency * 2), max_keepalive_connections=max(8, concurrency))
    headers = {"User-Agent": "GAIA/7.0 market-sensor-wave (+https://github.com/catears124/GAIA)"}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        limits=limits,
        follow_redirects=True,
        headers=headers,
    ) as client:
        outcomes = await asyncio.gather(*(fetch_sensor(client, spec, semaphore=semaphore) for spec in specs))
    postings: list[Posting] = []
    runs: list[SensorRun] = []
    for batch, run in outcomes:
        postings.extend(batch)
        runs.append(run)
    return postings, runs
