from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .api import TECH_CATEGORIES
from .classify import is_default_target
from .grouping import display_company, display_title, family_key
from .models import Posting, canonical_url
from .quality import normalize_locations
from .smb_ats_collectors import ISolvedHireCollector, JazzHRTargetCollector
from .snapshot_validation import validate_snapshot_file
from .static_snapshot import _responses_from_index, _validate_snapshot

DEFAULT_INVENTORY = Path(__file__).with_name("frontend") / "last-known-inventory.json"
VALID_REFRESH_STATUSES = frozenset({"loaded", "empty", "ok"})


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _max_time(values: list[object]) -> str | None:
    parsed = [value for value in (_parse_time(item) for item in values) if value is not None]
    return max(parsed).isoformat() if parsed else None


def _min_time(values: list[object]) -> str | None:
    parsed = [value for value in (_parse_time(item) for item in values) if value is not None]
    return min(parsed).isoformat() if parsed else None


def _shard(source: str, shards: int) -> int:
    digest = hashlib.sha1(source.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % max(1, shards)


def _slot_index(shards: int, now: datetime | None = None) -> int:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    quarter_hours = int(current.timestamp() // 900)
    return quarter_hours % max(1, shards)


def collectors_from_census(
    snapshot: dict[str, Any],
    *,
    shards: int,
    shard_index: int,
) -> list[ISolvedHireCollector | JazzHRTargetCollector]:
    rows = snapshot.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("census snapshot is missing candidates")
    found: dict[str, ISolvedHireCollector | JazzHRTargetCollector] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        if source.startswith("domain:isolvedhire:"):
            slug = source.removeprefix("domain:isolvedhire:")
            collector = ISolvedHireCollector(slug)
        elif source.startswith("domain:jazzhr:"):
            slug = source.removeprefix("domain:jazzhr:")
            collector = JazzHRTargetCollector(slug)
        else:
            continue
        if _shard(collector.name, shards) != shard_index:
            continue
        found[collector.name] = collector
    return list(found.values())


def _opening_first_seen(previous: dict[str, dict[str, object]], posting: Posting, now: str) -> str:
    existing = previous.get(posting.canonical_apply_url)
    if existing:
        value = existing.get("first_detected_at")
        if value:
            return str(value)
    return now


def _opening_from_posting(
    posting: Posting,
    *,
    previous: dict[str, dict[str, object]],
    now: str,
) -> dict[str, object]:
    return {
        "apply_url": posting.canonical_apply_url,
        "source": posting.source,
        "source_mode": "direct",
        "location": normalize_locations(posting.locations),
        "posted_at": _iso(posting.posted_at),
        "first_detected_at": _opening_first_seen(previous, posting, now),
    }


def _target_rank(value: object) -> int:
    return {"exact": 3, "source_confirmed": 2, "year_confirmed": 1}.get(str(value), 0)


def _rebuild_family(
    base: dict[str, object],
    openings: list[dict[str, object]],
    *,
    verified_at: str | None = None,
) -> dict[str, object]:
    unique: dict[str, dict[str, object]] = {}
    for opening in openings:
        url = str(opening.get("apply_url") or "")
        if not url:
            continue
        unique[canonical_url(url)] = dict(opening)
    rows = list(unique.values())
    direct = [row for row in rows if str(row.get("source_mode") or "") == "direct"]
    backstop = [row for row in rows if str(row.get("source_mode") or "") != "direct"]
    locations = normalize_locations(
        [
            str(location)
            for row in rows
            for location in (row.get("location") or [])
            if str(location).strip()
        ]
        or list(base.get("locations") or [])
    )
    result = dict(base)
    result.update(
        {
            "openings": rows,
            "opening_count": len(rows),
            "direct_openings": len(direct),
            "backstop_openings": len(backstop),
            "verified": bool(direct),
            "quality": "verified" if direct else "lead",
            "locations": locations,
            "latest_posted_at": _max_time([row.get("posted_at") for row in rows]),
            "first_detected_at": _min_time(
                [row.get("first_detected_at") for row in rows]
            )
            or result.get("first_detected_at"),
            "remote": any(
                "remote" in str(location).casefold() for location in locations
            ),
        }
    )
    if verified_at:
        result["last_verified_at"] = verified_at
    return result


def merge_refresh(
    previous: dict[str, Any],
    *,
    postings: list[Posting],
    refreshed_aliases: set[str],
    now: str,
) -> list[dict[str, object]]:
    raw_index = previous.get("family_index")
    if not isinstance(raw_index, list):
        raise ValueError("previous inventory is missing family_index")

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
            url = str(opening.get("apply_url") or "")
            if url:
                previous_openings[canonical_url(url)] = opening
            if str(opening.get("source") or "") not in refreshed_aliases:
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
                "latest_posted_at": _iso(max(posted_values)) if posted_values else None,
                "posted_precision": precision,
                "first_detected_at": now,
                "last_verified_at": now,
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
        families[key] = _rebuild_family(base, [*existing, *incoming], verified_at=now)

    return sorted(
        families.values(),
        key=lambda item: (
            _parse_time(item.get("latest_posted_at"))
            or _parse_time(item.get("first_detected_at"))
            or datetime.min.replace(tzinfo=UTC),
            _parse_time(item.get("last_verified_at"))
            or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )


async def refresh_collectors(
    collectors: list[ISolvedHireCollector | JazzHRTargetCollector],
    *,
    concurrency: int,
) -> tuple[list[Posting], set[str], dict[str, object]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    timeout = httpx.Timeout(float(os.getenv("GAIA_STATELESS_HTTP_TIMEOUT", "15")))
    limits = httpx.Limits(
        max_connections=max(32, concurrency * 2),
        max_keepalive_connections=max(16, concurrency),
    )
    headers = {
        "User-Agent": os.getenv(
            "GAIA_USER_AGENT",
            "GAIA/6.0 stateless-inventory (+https://github.com/catears124/GAIA)",
        )
    }
    postings: list[Posting] = []
    refreshed_aliases: set[str] = set()
    statuses: Counter[str] = Counter()
    providers: Counter[str] = Counter()

    async with httpx.AsyncClient(
        timeout=timeout, limits=limits, headers=headers, follow_redirects=True
    ) as client:

        async def run(collector: ISolvedHireCollector | JazzHRTargetCollector):
            async with semaphore:
                try:
                    return collector, await collector.collect(client), None
                except Exception as exc:  # noqa: BLE001 - isolate one tenant
                    return collector, None, repr(exc)

        outcomes = await asyncio.gather(*(run(collector) for collector in collectors))

    failures: list[dict[str, str]] = []
    for collector, result, exception in outcomes:
        provider = collector.name.split(":", 1)[0]
        providers[provider] += 1
        if result is None:
            statuses["exception"] += 1
            failures.append({"source": collector.name, "error": exception or "unknown"})
            continue
        statuses[str(result.status or "unknown")] += 1
        if result.complete and result.error is None and result.status in VALID_REFRESH_STATUSES:
            refreshed_aliases.add(collector.name)
            if isinstance(collector, ISolvedHireCollector):
                refreshed_aliases.add(f"domain:isolvedhire:{collector.slug}")
            else:
                refreshed_aliases.add(f"domain:jazzhr:{collector.slug}")
            postings.extend(
                posting
                for posting in result.postings
                if is_default_target(posting) and posting.category in TECH_CATEGORIES
            )
        elif result.error:
            failures.append({"source": collector.name, "error": str(result.error)[:200]})

    deduped = {posting.posting_key: posting for posting in postings}
    summary: dict[str, object] = {
        "selected_sources": len(collectors),
        "refreshed_sources": len(refreshed_aliases) // 2,
        "target_postings": len(deduped),
        "providers": dict(sorted(providers.items())),
        "statuses": dict(sorted(statuses.items())),
        "failures": failures[:50],
    }
    return list(deduped.values()), refreshed_aliases, summary


def build_health(summary: dict[str, object], now: str) -> dict[str, object]:
    selected = int(summary.get("selected_sources") or 0)
    refreshed = int(summary.get("refreshed_sources") or 0)
    unhealthy = max(0, selected - refreshed)
    fresh_percent = round(100 * refreshed / selected, 1) if selected else 0.0
    inventory = {
        "total": selected,
        "running": 0,
        "never_completed": unhealthy,
        "overdue": 0,
        "degraded": unhealthy,
        "fresh": refreshed,
        "unhealthy": unhealthy,
        "historical": 0,
        "latest_activity_at": now,
        "coverage_watermark": now if refreshed else None,
        "freshness_floor_seconds": 0,
        "fresh_percent": fresh_percent,
        "healthy": selected > 0 and unhealthy == 0,
        "stateless": True,
        "shard": summary.get("shard"),
        "shards": summary.get("shards"),
    }
    return {
        "ok": refreshed > 0,
        "read_only": False,
        "running": False,
        "stale": False,
        "generated_at": now,
        "progress": {
            "mode": "stateless-tenant-sweep",
            "stage": "complete",
            "completed": refreshed,
            "total": selected,
            "current": None,
            "started_at": None,
            "elapsed_seconds": 0,
        },
        "last_summary": summary,
        "data": {
            "last_run": {"finished_at": now, "status": "ok" if refreshed else "degraded"},
            "last_success_at": now if refreshed else None,
            "sources": selected,
            "failing_sources": unhealthy,
        },
        "inventory": inventory,
    }


async def run(
    *,
    census_path: Path,
    previous_path: Path,
    output_path: Path,
    shards: int,
    shard_index: int,
    concurrency: int,
) -> dict[str, object]:
    census = json.loads(census_path.read_text(encoding="utf-8"))
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    if not isinstance(census, dict) or not isinstance(previous, dict):
        raise ValueError("census and previous inventory must be JSON objects")
    collectors = collectors_from_census(
        census, shards=max(1, shards), shard_index=shard_index
    )
    postings, refreshed_aliases, summary = await refresh_collectors(
        collectors, concurrency=max(1, concurrency)
    )
    now = datetime.now(UTC).isoformat()
    summary.update({"shard": shard_index, "shards": shards})
    families = merge_refresh(
        previous,
        postings=postings,
        refreshed_aliases=refreshed_aliases,
        now=now,
    )
    health = build_health(summary, now)
    payload: dict[str, object] = {
        "schema_version": 2,
        "generated_at": now,
        "source_activity_at": now,
        "max_stale_seconds": 86_400,
        "family_index": families,
        "family_index_total": len(families),
        "family_index_complete": True,
        "responses": _responses_from_index(families, health),
        "stateless_refresh": summary,
    }
    _validate_snapshot(payload, previous)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8"
    )
    temporary.replace(output_path)
    validate_snapshot_file(output_path, max_age_hours=1)
    return {
        **summary,
        "families": len(families),
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh GAIA static inventory directly from census-discovered ATS boards"
    )
    parser.add_argument("--census", type=Path, required=True)
    parser.add_argument("--previous", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--shards", type=int, default=int(os.getenv("GAIA_STATELESS_SHARDS", "16")))
    parser.add_argument("--shard-index", type=int, default=None)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("GAIA_STATELESS_CONCURRENCY", "48")),
    )
    args = parser.parse_args()
    shards = max(1, args.shards)
    shard_index = args.shard_index if args.shard_index is not None else _slot_index(shards)
    if shard_index < 0 or shard_index >= shards:
        raise ValueError(f"shard-index must be in [0,{shards - 1}]")
    summary = asyncio.run(
        run(
            census_path=args.census,
            previous_path=args.previous,
            output_path=args.output,
            shards=shards,
            shard_index=shard_index,
            concurrency=max(1, args.concurrency),
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
