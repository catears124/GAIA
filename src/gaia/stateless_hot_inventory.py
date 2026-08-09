from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .api import TECH_CATEGORIES
from .classify import is_default_target
from .collectors import Collector
from .discovery import collectors_from_registry, registry_collectors
from .grouping import display_company, display_title, family_key
from .models import Posting, canonical_url
from .quality import normalize_locations
from .snapshot_validation import validate_snapshot_file
from .stateless_inventory import _parse_time, _rebuild_family, _target_rank
from .static_snapshot import _responses_from_index, _validate_snapshot

DEFAULT_INVENTORY = Path(__file__).with_name("frontend") / "last-known-inventory.json"

# These markdown feeds are explicitly Summer 2027 lists. Their rows often omit
# "Summer 2027" from otherwise-valid internship titles, so the feed itself is
# cycle evidence when the employer title does not contradict it.
SUMMER_2027_REGISTRIES = frozenset(
    {
        "registry:simplify-2027",
        "registry:zapply-2027",
        "registry:speedy-swe-2027-us",
        "registry:speedy-swe-2027-intl",
    }
)
REJECTED_TARGETS = frozenset({"not_internship", "wrong_year", "wrong_season"})


def _source_mode(posting: Posting) -> str:
    if posting.source_mode == "verification":
        return "direct"
    if posting.source_mode == "verification-lead" or posting.source.startswith("registry:"):
        return "registry"
    return posting.source_mode or "direct"


def _opening_from_posting(
    posting: Posting,
    *,
    previous: dict[str, dict[str, object]],
    now: str,
) -> dict[str, object]:
    existing = previous.get(posting.canonical_apply_url)
    first_detected = (
        str(existing.get("first_detected_at"))
        if existing and existing.get("first_detected_at")
        else now
    )
    posted_at = posting.posted_at.astimezone(UTC).isoformat() if posting.posted_at else None
    return {
        "apply_url": posting.canonical_apply_url,
        "source": posting.source,
        "source_mode": _source_mode(posting),
        "location": normalize_locations(posting.locations),
        "posted_at": posted_at,
        "first_detected_at": first_detected,
    }


def _source_confirms_summer_2027(posting: Posting) -> Posting:
    if posting.source not in SUMMER_2027_REGISTRIES:
        return posting
    if posting.target_match != "unknown":
        return posting
    if posting.category not in TECH_CATEGORIES:
        return posting
    return replace(
        posting,
        year=2027,
        season="summer",
        target_match="source_confirmed",
    )


def _direct_target(posting: Posting, registry_by_url: dict[str, Posting]) -> Posting:
    if is_default_target(posting) or posting.target_match in REJECTED_TARGETS:
        return posting
    lead = registry_by_url.get(posting.canonical_apply_url)
    if lead is None or not is_default_target(lead):
        return posting
    return replace(
        posting,
        year=lead.year or 2027,
        season=lead.season or "summer",
        target_match=lead.target_match,
        category=posting.category if posting.category in TECH_CATEGORIES else lead.category,
    )


def merge_mixed_refresh(
    previous: dict[str, Any],
    *,
    postings: list[Posting],
    refreshed_aliases: set[str],
    now: str,
) -> list[dict[str, object]]:
    raw_index = previous.get("family_index")
    if not isinstance(raw_index, list):
        raise ValueError("previous inventory is missing family_index")

    incoming_urls = {posting.canonical_apply_url for posting in postings if posting.apply_url}
    families: dict[str, dict[str, object]] = {}
    previous_openings: dict[str, dict[str, object]] = {}

    for raw in raw_index:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("family_key") or "")
        if not key:
            continue
        kept: list[dict[str, object]] = []
        for opening in raw.get("openings") or []:
            if not isinstance(opening, dict):
                continue
            raw_url = str(opening.get("apply_url") or "")
            normalized_url = canonical_url(raw_url) if raw_url else ""
            if normalized_url:
                previous_openings[normalized_url] = dict(opening)
            if str(opening.get("source") or "") in refreshed_aliases:
                continue
            if normalized_url and normalized_url in incoming_urls:
                # A fresher observation of the same application supersedes a registry
                # lead even when the employer title groups to a different family.
                continue
            kept.append(dict(opening))
        if kept:
            families[key] = _rebuild_family(dict(raw), kept)

    grouped: dict[str, list[Posting]] = defaultdict(list)
    for posting in postings:
        grouped[family_key(posting)].append(posting)

    for key, group in grouped.items():
        incoming = [
            _opening_from_posting(posting, previous=previous_openings, now=now)
            for posting in group
        ]
        base = families.get(key)
        if base is None:
            strongest = max(group, key=lambda item: _target_rank(item.target_match))
            posted_values = [item.posted_at for item in group if item.posted_at is not None]
            precision = "unknown"
            if any(item.posted_precision == "timestamp" for item in group):
                precision = "timestamp"
            elif any(item.posted_precision == "date" for item in group):
                precision = "date"
            base = {
                "family_key": key,
                "title": display_title(group),
                "company": display_company(strongest.company),
                "category": strongest.category,
                "target_match": strongest.target_match,
                "year": strongest.year,
                "season": strongest.season,
                "locations": normalize_locations(
                    [location for item in group for location in item.locations]
                ),
                "latest_posted_at": (
                    max(posted_values).astimezone(UTC).isoformat() if posted_values else None
                ),
                "posted_precision": precision,
                "first_detected_at": now,
                "last_verified_at": (
                    now if any(_source_mode(item) == "direct" for item in group) else None
                ),
                "remote": any(
                    "remote" in location.casefold()
                    for item in group
                    for location in item.locations
                ),
                "openings": [],
            }
        else:
            strongest = max(group, key=lambda item: _target_rank(item.target_match))
            if _target_rank(strongest.target_match) > _target_rank(base.get("target_match")):
                base["target_match"] = strongest.target_match
                base["year"] = strongest.year
                base["season"] = strongest.season
            if base.get("category") in {None, "", "other"} and strongest.category != "other":
                base["category"] = strongest.category
        existing = [
            dict(row) for row in (base.get("openings") or []) if isinstance(row, dict)
        ]
        verified_at = now if any(_source_mode(item) == "direct" for item in group) else None
        families[key] = _rebuild_family(
            base,
            [*existing, *incoming],
            verified_at=verified_at,
        )

    return sorted(
        families.values(),
        key=lambda item: (
            _parse_time(item.get("latest_posted_at"))
            or _parse_time(item.get("first_detected_at"))
            or datetime.min.replace(tzinfo=UTC),
            bool(item.get("verified")),
            _parse_time(item.get("last_verified_at"))
            or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )


def snapshot_seed_postings(snapshot: dict[str, Any]) -> list[Posting]:
    rows = snapshot.get("family_index")
    if not isinstance(rows, list):
        return []
    output: dict[str, Posting] = {}
    for family in rows:
        if not isinstance(family, dict):
            continue
        company = str(family.get("company") or "")
        title = str(family.get("title") or "")
        if not company or not title:
            continue
        for opening in family.get("openings") or []:
            if not isinstance(opening, dict):
                continue
            url = str(opening.get("apply_url") or "")
            if not url:
                continue
            key = canonical_url(url)
            output[key] = Posting(
                company=company,
                title=title,
                apply_url=url,
                source="snapshot-seed",
                source_id=key,
                locations=[
                    str(value)
                    for value in opening.get("location") or family.get("locations") or []
                ],
                source_mode="registry",
            )
    return list(output.values())


async def _collect(
    collectors: list[Collector],
    *,
    concurrency: int,
) -> tuple[list[Posting], set[str], dict[str, object]]:
    if not collectors:
        return [], set(), {
            "selected_sources": 0,
            "refreshed_sources": 0,
            "postings": 0,
            "statuses": {},
            "failures": [],
        }

    semaphore = asyncio.Semaphore(max(1, concurrency))
    timeout = httpx.Timeout(float(os.getenv("GAIA_STATELESS_HOT_HTTP_TIMEOUT", "30")))
    limits = httpx.Limits(
        max_connections=max(32, concurrency * 2),
        max_keepalive_connections=max(16, concurrency),
    )
    headers = {
        "User-Agent": os.getenv(
            "GAIA_USER_AGENT",
            "GAIA/6.0 stateless-hot-inventory (+https://github.com/catears124/GAIA)",
        )
    }

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        headers=headers,
        follow_redirects=True,
    ) as client:

        async def one(collector: Collector):
            async with semaphore:
                try:
                    return collector, await collector.collect(client), None
                except Exception as exc:  # noqa: BLE001 - isolate one source
                    return collector, None, repr(exc)

        outcomes = await asyncio.gather(*(one(collector) for collector in collectors))

    postings: list[Posting] = []
    refreshed: set[str] = set()
    statuses: Counter[str] = Counter()
    failures: list[dict[str, str]] = []
    for collector, result, exception in outcomes:
        if result is None:
            statuses["exception"] += 1
            failures.append({"source": collector.name, "error": exception or "unknown"})
            continue
        status = str(result.status or "ok")
        statuses[status] += 1
        if result.complete and result.error is None and status not in {
            "broken",
            "blocked",
            "partial",
            "truncated",
        }:
            refreshed.add(result.source)
        postings.extend(result.postings)
        if result.error:
            failures.append({"source": result.source, "error": str(result.error)[:240]})

    deduped = {posting.posting_key: posting for posting in postings}
    return list(deduped.values()), refreshed, {
        "selected_sources": len(collectors),
        "refreshed_sources": len(refreshed),
        "postings": len(deduped),
        "statuses": dict(sorted(statuses.items())),
        "failures": failures[:50],
    }


def _direct_collectors(
    registry_postings: list[Posting],
    snapshot: dict[str, Any],
) -> list[Collector]:
    fresh = collectors_from_registry(registry_postings)
    durable = [
        collector
        for collector in collectors_from_registry(snapshot_seed_postings(snapshot))
        if collector.name.startswith(("greenhouse:", "lever:", "ashby:", "workday:"))
    ]

    merged: dict[str, Collector] = {}
    # Durable board identities fill gaps, but fresh registry evidence wins because
    # SchemaPageCollector carries current lead URLs needed for verification.
    for collector in [*durable, *fresh]:
        if getattr(collector, "scope", "current") == "historical":
            continue
        merged[collector.name] = collector

    board: list[Collector] = []
    schema: list[Collector] = []
    workday: list[Collector] = []
    other: list[Collector] = []
    for collector in merged.values():
        if collector.name.startswith(("greenhouse:", "lever:", "ashby:")):
            board.append(collector)
        elif collector.name.startswith("schema:"):
            schema.append(collector)
        elif collector.name.startswith("workday:"):
            workday.append(collector)
        else:
            other.append(collector)

    board.sort(key=lambda item: item.name)
    schema.sort(key=lambda item: item.name)
    workday.sort(key=lambda item: item.name)
    other.sort(key=lambda item: item.name)

    schema_limit = max(0, int(os.getenv("GAIA_STATELESS_HOT_SCHEMA_LIMIT", "120")))
    workday_limit = max(0, int(os.getenv("GAIA_STATELESS_HOT_WORKDAY_LIMIT", "24")))
    max_collectors = max(1, int(os.getenv("GAIA_STATELESS_HOT_MAX_COLLECTORS", "500")))
    selected = [
        *board,
        *schema[:schema_limit],
        *workday[:workday_limit],
        *other,
    ]
    return selected[:max_collectors]


def _previous_health(snapshot: dict[str, Any], now: str) -> dict[str, object]:
    responses = snapshot.get("responses")
    health = dict(responses.get("/api/health") or {}) if isinstance(responses, dict) else {}
    health["generated_at"] = now
    health["stale"] = False
    progress = dict(health.get("progress") or {})
    progress.update({"mode": "stateless-hot-sources", "stage": "complete"})
    health["progress"] = progress
    return health


async def run(
    *,
    previous_path: Path,
    output_path: Path,
    concurrency: int,
) -> dict[str, object]:
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    if not isinstance(previous, dict):
        raise ValueError("previous inventory must be a JSON object")

    registry_raw, registry_aliases, registry_summary = await _collect(
        registry_collectors(),
        concurrency=min(max(1, concurrency), 12),
    )
    registry_postings = [
        promoted
        for posting in registry_raw
        if (promoted := _source_confirms_summer_2027(posting)).category in TECH_CATEGORIES
        and is_default_target(promoted)
    ]
    registry_by_url = {
        posting.canonical_apply_url: posting for posting in registry_postings
    }

    now = datetime.now(UTC).isoformat()
    registry_snapshot = dict(previous)
    registry_snapshot["family_index"] = merge_mixed_refresh(
        previous,
        postings=registry_postings,
        refreshed_aliases=registry_aliases,
        now=now,
    )

    direct_collectors = _direct_collectors(registry_postings, registry_snapshot)
    direct_raw, direct_aliases, direct_summary = await _collect(
        direct_collectors,
        concurrency=max(1, concurrency),
    )
    direct_postings: list[Posting] = []
    for posting in direct_raw:
        candidate = _direct_target(posting, registry_by_url)
        if candidate.category in TECH_CATEGORIES and is_default_target(candidate):
            direct_postings.append(candidate)

    families = merge_mixed_refresh(
        registry_snapshot,
        postings=direct_postings,
        refreshed_aliases=direct_aliases,
        now=datetime.now(UTC).isoformat(),
    )
    generated_at = datetime.now(UTC).isoformat()
    health = _previous_health(previous, generated_at)
    payload: dict[str, object] = {
        "schema_version": 2,
        "generated_at": generated_at,
        "source_activity_at": generated_at,
        "max_stale_seconds": 86_400,
        "family_index": families,
        "family_index_total": len(families),
        "family_index_complete": True,
        "responses": _responses_from_index(families, health),
        "stateless_hot_refresh": {
            "registry": registry_summary,
            "registry_target_postings": len(registry_postings),
            "direct": direct_summary,
            "direct_target_postings": len(direct_postings),
        },
    }
    _validate_snapshot(payload, previous)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), default=str),
        encoding="utf-8",
    )
    temporary.replace(output_path)
    validate_snapshot_file(output_path, max_age_hours=1)

    return {
        "families": len(families),
        "registry_sources": registry_summary["selected_sources"],
        "registry_postings": registry_summary["postings"],
        "registry_target_postings": len(registry_postings),
        "direct_sources": direct_summary["selected_sources"],
        "direct_postings": direct_summary["postings"],
        "direct_target_postings": len(direct_postings),
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh GAIA from high-yield 2027 registries and their employer sources"
    )
    parser.add_argument("--previous", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("GAIA_STATELESS_HOT_CONCURRENCY", "32")),
    )
    args = parser.parse_args()
    summary = asyncio.run(
        run(
            previous_path=args.previous,
            output_path=args.output,
            concurrency=max(1, args.concurrency),
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
