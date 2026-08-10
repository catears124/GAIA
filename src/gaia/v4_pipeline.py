from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .classify import is_default_target
from .collectors import Collector
from .discovery import collectors_from_registry
from .grouping import display_company, display_title, family_key
from .models import Posting, canonical_url
from .quality import normalize_locations
from .v4_sensors import SensorRun, fetch_all_sensors
from .v4_snapshot import activity as family_activity
from .v4_snapshot import responses as snapshot_responses
from .v4_snapshot import stats as snapshot_stats
from .v4_snapshot import timestamp

DEFAULT_INVENTORY = Path(__file__).with_name("frontend") / "last-known-inventory.json"
REJECTED_TARGETS = {"not_internship", "wrong_year", "wrong_season"}
DIRECT_PREFIXES = ("greenhouse:", "lever:", "ashby:", "workday:", "google-careers")
TARGET_RANK = {
    "unknown": 0,
    "source_confirmed": 1,
    "year_confirmed": 2,
    "exact": 3,
}


@dataclass(slots=True)
class Observation:
    posting: Posting
    first_seen_at: datetime
    verified_at: datetime | None = None


@dataclass(slots=True)
class AggregatedOpening:
    posting: Posting
    public: dict[str, object]
    market_event_at: datetime
    first_seen_at: datetime
    verified_at: datetime | None
    direct: bool


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _direct_mode(mode: str, source: str = "") -> bool:
    value = (mode or "direct").casefold()
    if value in {"registry", "market-sensor", "external-index", "verification-lead", "universe-seed", "lead"}:
        return False
    if source.startswith(("sensor:", "registry:")):
        return False
    return True


def _source_mode(posting: Posting) -> str:
    if posting.source_mode == "verification":
        return "direct"
    if posting.source_mode in {"verification-lead", "registry", "market-sensor"} or posting.source.startswith(("registry:", "sensor:")):
        return "market-sensor"
    return posting.source_mode or "direct"


def _target_rank(value: object) -> int:
    return TARGET_RANK.get(str(value or "unknown"), 0)


def _promote_direct(posting: Posting, sensor_by_url: dict[str, list[Posting]]) -> Posting:
    """Let sensor cycle evidence classify a URL, never let it confer verification."""
    if is_default_target(posting) or posting.target_match in REJECTED_TARGETS:
        return posting
    leads = sensor_by_url.get(posting.canonical_apply_url) or []
    candidates = [lead for lead in leads if is_default_target(lead)]
    if not candidates:
        return posting
    lead = max(candidates, key=lambda item: (_target_rank(item.target_match), item.market_event_at))
    return replace(
        posting,
        year=lead.year or 2027,
        season=lead.season or "summer",
        target_match=lead.target_match,
        category=posting.category if posting.category not in {"", "other"} else lead.category,
    )


async def _collect(
    collectors: list[Collector],
    *,
    concurrency: int,
) -> tuple[list[Posting], set[str], dict[str, object]]:
    if not collectors:
        return [], set(), {"selected": 0, "refreshed": 0, "postings": 0, "statuses": {}, "failures": []}

    semaphore = asyncio.Semaphore(max(1, concurrency))
    timeout = httpx.Timeout(float(os.getenv("GAIA_V4_VERIFY_TIMEOUT", "30")))
    limits = httpx.Limits(
        max_connections=max(32, concurrency * 2),
        max_keepalive_connections=max(16, concurrency),
    )
    headers = {"User-Agent": "GAIA/7.0 verifier (+https://github.com/catears124/GAIA)"}

    async with httpx.AsyncClient(timeout=timeout, limits=limits, headers=headers, follow_redirects=True) as client:
        async def one(collector: Collector):
            async with semaphore:
                try:
                    return collector, await collector.collect(client), None
                except Exception as exc:  # noqa: BLE001 - isolate one employer source
                    return collector, None, repr(exc)

        outcomes = await asyncio.gather(*(one(collector) for collector in collectors))

    postings: list[Posting] = []
    refreshed: set[str] = set()
    statuses: Counter[str] = Counter()
    failures: list[dict[str, str]] = []
    for collector, result, error in outcomes:
        if result is None:
            statuses["exception"] += 1
            failures.append({"source": collector.name, "error": str(error or "unknown")[:300]})
            continue
        status = str(result.status or "ok")
        statuses[status] += 1
        if result.complete and result.error is None and status not in {"broken", "blocked", "partial", "truncated"}:
            refreshed.add(result.source)
        postings.extend(result.postings)
        if result.error:
            failures.append({"source": result.source, "error": str(result.error)[:300]})

    deduped = {posting.posting_key: posting for posting in postings}
    return list(deduped.values()), refreshed, {
        "selected": len(collectors),
        "refreshed": len(refreshed),
        "postings": len(deduped),
        "statuses": dict(sorted(statuses.items())),
        "failures": failures[:100],
    }


def _previous_first_seen(snapshot: dict[str, Any]) -> dict[str, datetime]:
    output: dict[str, datetime] = {}
    for family in snapshot.get("family_index") or []:
        if not isinstance(family, dict):
            continue
        family_seen = timestamp(family.get("first_detected_at"))
        for opening in family.get("openings") or []:
            if not isinstance(opening, dict):
                continue
            raw_url = str(opening.get("apply_url") or "")
            if not raw_url:
                continue
            seen = timestamp(opening.get("first_detected_at")) or family_seen
            if seen is None:
                continue
            key = canonical_url(raw_url)
            existing = output.get(key)
            if existing is None or seen < existing:
                output[key] = seen
    return output


def _previous_observations(snapshot: dict[str, Any], now: datetime) -> list[Observation]:
    output: list[Observation] = []
    direct_ttl = timedelta(hours=max(1, int(os.getenv("GAIA_V4_DIRECT_TTL_HOURS", "72"))))
    lead_ttl = timedelta(hours=max(1, int(os.getenv("GAIA_V4_LEAD_TTL_HOURS", "24"))))

    for family in snapshot.get("family_index") or []:
        if not isinstance(family, dict):
            continue
        company = str(family.get("company") or "")
        title = str(family.get("title") or "")
        if not company or not title:
            continue
        family_seen = timestamp(family.get("first_detected_at")) or now
        family_verified = timestamp(family.get("last_verified_at"))
        category = str(family.get("category") or "other")
        year = int(family["year"]) if family.get("year") else None
        season = str(family.get("season")) if family.get("season") else None
        target_match = str(family.get("target_match") or "unknown")

        for opening in family.get("openings") or []:
            if not isinstance(opening, dict):
                continue
            url = str(opening.get("apply_url") or "")
            if not url:
                continue
            first_seen = timestamp(opening.get("first_detected_at")) or family_seen
            evidence = opening.get("evidence")
            evidence_rows = evidence if isinstance(evidence, list) and evidence else [opening]
            for row in evidence_rows:
                if not isinstance(row, dict):
                    continue
                source = str(row.get("source") or opening.get("source") or "previous")
                mode = str(row.get("source_mode") or opening.get("source_mode") or "direct")
                verified_at = timestamp(row.get("verified_at")) or family_verified if _direct_mode(mode, source) else None
                observed = timestamp(row.get("observed_at")) or verified_at or first_seen
                ttl = direct_ttl if _direct_mode(mode, source) else lead_ttl
                freshness_anchor = verified_at or observed or first_seen
                if freshness_anchor < now - ttl:
                    continue
                posting = Posting(
                    company=company,
                    title=title,
                    apply_url=url,
                    source=source,
                    source_id=str(row.get("source_id") or canonical_url(url)),
                    locations=[str(value) for value in opening.get("location") or family.get("locations") or []],
                    source_mode=mode,
                    posted_at=timestamp(row.get("posted_at") or opening.get("posted_at")),
                    posted_precision=str(row.get("posted_precision") or family.get("posted_precision") or "unknown"),
                    posted_confidence=str(row.get("posted_confidence") or "unknown"),
                    sensor_reported_at=timestamp(row.get("sensor_reported_at") or opening.get("sensor_reported_at")),
                    sensor_reported_raw=str(row.get("sensor_reported_raw")) if row.get("sensor_reported_raw") else None,
                    sensor_precision=str(row.get("sensor_precision") or opening.get("sensor_precision") or "unknown"),
                    sensor_confidence=str(row.get("sensor_confidence") or "unknown"),
                    observed_at=observed,
                    category=category,
                    season=season,
                    year=year,
                    target_match=target_match,
                )
                output.append(Observation(posting=posting, first_seen_at=first_seen, verified_at=verified_at))
    return output


def _seed_previous_boards(snapshot: dict[str, Any]) -> list[Posting]:
    seeds: dict[str, Posting] = {}
    for family in snapshot.get("family_index") or []:
        if not isinstance(family, dict):
            continue
        for opening in family.get("openings") or []:
            if not isinstance(opening, dict):
                continue
            url = str(opening.get("apply_url") or "")
            if not url or not _direct_mode(str(opening.get("source_mode") or "direct"), str(opening.get("source") or "")):
                continue
            key = canonical_url(url)
            seeds[key] = Posting(
                company=str(family.get("company") or "Unknown"),
                title=str(family.get("title") or "Intern"),
                apply_url=url,
                source="v4-durable-seed",
                source_id=key,
                locations=[str(value) for value in opening.get("location") or []],
                source_mode="market-sensor",
                category=str(family.get("category") or "other"),
                season=str(family.get("season")) if family.get("season") else None,
                year=int(family["year"]) if family.get("year") else None,
                target_match=str(family.get("target_match") or "unknown"),
            )
    return list(seeds.values())


def _verification_collectors(sensor_postings: list[Posting], snapshot: dict[str, Any]) -> list[Collector]:
    fresh = collectors_from_registry(sensor_postings)
    durable = [
        collector
        for collector in collectors_from_registry(_seed_previous_boards(snapshot))
        if collector.name.startswith(DIRECT_PREFIXES)
    ]

    # Fresh sensor-derived collectors are never displaced by the durable board lane.
    # This is the opposite of the old source-count objective.
    merged: dict[str, Collector] = {}
    for collector in fresh:
        merged[collector.name] = collector
    durable_limit = max(0, int(os.getenv("GAIA_V4_DURABLE_BOARD_LIMIT", "1200")))
    for collector in durable[:durable_limit]:
        merged.setdefault(collector.name, collector)

    values = list(merged.values())
    fresh_names = {collector.name for collector in fresh}

    def priority(collector: Collector) -> tuple[int, int, str]:
        current = 0 if collector.name in fresh_names else 1
        if collector.name.startswith(("greenhouse:", "lever:", "ashby:")):
            kind = 0
        elif collector.name.startswith("schema:"):
            kind = 1
        elif collector.name.startswith("workday:"):
            kind = 2
        else:
            kind = 3
        return current, kind, collector.name

    values.sort(key=priority)
    total_limit = max(len(fresh), int(os.getenv("GAIA_V4_VERIFY_COLLECTOR_LIMIT", "2000")))
    return values[:total_limit]


def _incoming_observations(
    postings: list[Posting],
    *,
    first_seen_by_url: dict[str, datetime],
    now: datetime,
) -> list[Observation]:
    output: list[Observation] = []
    for posting in postings:
        key = posting.canonical_apply_url
        first_seen = first_seen_by_url.get(key, posting.observed_at or now)
        output.append(
            Observation(
                posting=posting,
                first_seen_at=first_seen,
                verified_at=now if _direct_mode(_source_mode(posting), posting.source) else None,
            )
        )
    return output


def _aggregate_opening(records: list[Observation]) -> AggregatedOpening:
    direct_records = [record for record in records if _direct_mode(_source_mode(record.posting), record.posting.source)]
    best_pool = direct_records or records
    best = max(best_pool, key=lambda record: (_target_rank(record.posting.target_match), record.posting.market_event_at, record.posting.observed_at))

    posted_candidates = [(record.posting.posted_at, record) for record in direct_records if record.posting.posted_at]
    sensor_candidates = [(record.posting.sensor_reported_at, record) for record in records if record.posting.sensor_reported_at]
    posted_pair = max(posted_candidates, default=None, key=lambda pair: pair[0])
    sensor_pair = max(sensor_candidates, default=None, key=lambda pair: pair[0])
    posted_at = posted_pair[0] if posted_pair else None
    sensor_at = sensor_pair[0] if sensor_pair else None
    first_seen = min(record.first_seen_at for record in records)
    verified_values = [record.verified_at for record in direct_records if record.verified_at]
    verified_at = max(verified_values) if verified_values else None
    market_event = posted_at or sensor_at or first_seen
    event_kind = "employer-posted" if posted_at else ("sensor-reported" if sensor_at else "first-seen")
    event_precision = (
        posted_pair[1].posting.posted_precision
        if posted_pair
        else sensor_pair[1].posting.sensor_precision if sensor_pair else "timestamp"
    )

    locations = normalize_locations([location for record in records for location in record.posting.locations])
    evidence: list[dict[str, object]] = []
    seen_sources: set[tuple[str, str]] = set()
    for record in sorted(records, key=lambda value: value.posting.observed_at, reverse=True):
        mode = _source_mode(record.posting)
        identity = (record.posting.source, mode)
        if identity in seen_sources:
            continue
        seen_sources.add(identity)
        evidence.append(
            {
                "source": record.posting.source,
                "source_mode": mode,
                "source_id": record.posting.source_id,
                "posted_at": _iso(record.posting.posted_at),
                "posted_precision": record.posting.posted_precision,
                "posted_confidence": record.posting.posted_confidence,
                "sensor_reported_at": _iso(record.posting.sensor_reported_at),
                "sensor_reported_raw": record.posting.sensor_reported_raw,
                "sensor_precision": record.posting.sensor_precision,
                "sensor_confidence": record.posting.sensor_confidence,
                "observed_at": _iso(record.posting.observed_at),
                "verified_at": _iso(record.verified_at),
            }
        )

    posting = replace(
        best.posting,
        locations=locations,
        source=best.posting.source,
        source_mode="direct" if direct_records else "market-sensor",
        posted_at=posted_at,
        sensor_reported_at=sensor_at,
    )
    public: dict[str, object] = {
        "apply_url": posting.canonical_apply_url,
        "source": best.posting.source,
        "source_mode": "direct" if direct_records else "market-sensor",
        "location": locations,
        "posted_at": _iso(posted_at),
        "sensor_reported_at": _iso(sensor_at),
        "market_event_at": _iso(market_event),
        "market_event_kind": event_kind,
        "market_event_precision": event_precision,
        "first_detected_at": _iso(first_seen),
        "verified_at": _iso(verified_at),
        "evidence_count": len(evidence),
        "evidence": evidence,
    }
    return AggregatedOpening(
        posting=posting,
        public=public,
        market_event_at=market_event,
        first_seen_at=first_seen,
        verified_at=verified_at,
        direct=bool(direct_records),
    )


def _build_families(observations: list[Observation]) -> list[dict[str, object]]:
    by_url: dict[str, list[Observation]] = defaultdict(list)
    for record in observations:
        if record.posting.apply_url:
            by_url[record.posting.canonical_apply_url].append(record)
    openings = [_aggregate_opening(records) for records in by_url.values()]

    grouped: dict[str, list[AggregatedOpening]] = defaultdict(list)
    for opening in openings:
        grouped[family_key(opening.posting)].append(opening)

    families: list[dict[str, object]] = []
    for key, group in grouped.items():
        postings = [opening.posting for opening in group]
        strongest = max(postings, key=lambda item: (_target_rank(item.target_match), item.market_event_at))
        event_opening = max(group, key=lambda item: item.market_event_at)
        direct = [opening for opening in group if opening.direct]
        sensor_only = [opening for opening in group if not opening.direct]
        posted_values = [opening.posting.posted_at for opening in group if opening.posting.posted_at]
        sensor_values = [opening.posting.sensor_reported_at for opening in group if opening.posting.sensor_reported_at]
        verified_values = [opening.verified_at for opening in direct if opening.verified_at]
        all_locations = normalize_locations([location for opening in group for location in opening.posting.locations])
        sensor_sources = sorted(
            {
                str(evidence.get("source"))
                for opening in group
                for evidence in opening.public.get("evidence") or []
                if isinstance(evidence, dict) and str(evidence.get("source") or "").startswith("sensor:")
            }
        )
        group.sort(key=lambda opening: (opening.market_event_at, opening.direct), reverse=True)
        families.append(
            {
                "family_key": key,
                "title": display_title(postings),
                "company": display_company(strongest.company),
                "category": strongest.category,
                "target_match": strongest.target_match,
                "year": strongest.year,
                "season": strongest.season,
                "locations": all_locations,
                "opening_count": len(group),
                "direct_openings": len(direct),
                "backstop_openings": len(sensor_only),
                "verified": bool(direct),
                "quality": "employer" if direct else "lead",
                "latest_posted_at": _iso(max(posted_values)) if posted_values else None,
                "latest_sensor_reported_at": _iso(max(sensor_values)) if sensor_values else None,
                "market_event_at": _iso(event_opening.market_event_at),
                "market_event_kind": event_opening.public.get("market_event_kind"),
                "market_event_precision": event_opening.public.get("market_event_precision"),
                "market_first_seen_at": _iso(event_opening.first_seen_at),
                "posted_precision": (
                    event_opening.posting.posted_precision
                    if event_opening.posting.posted_at
                    else "unknown"
                ),
                "first_detected_at": _iso(min(opening.first_seen_at for opening in group)),
                "last_verified_at": _iso(max(verified_values)) if verified_values else None,
                "remote": any("remote" in location.casefold() for location in all_locations),
                "sensor_sources": sensor_sources,
                "evidence_count": sum(int(opening.public.get("evidence_count") or 0) for opening in group),
                "openings": [opening.public for opening in group],
            }
        )
    families.sort(key=lambda item: (family_activity(item), bool(item.get("verified"))), reverse=True)
    return families


def _health(
    *,
    runs: list[SensorRun],
    families: list[dict[str, object]],
    verify_summary: dict[str, object],
    generated_at: datetime,
) -> dict[str, object]:
    successful = [run for run in runs if run.status == "ok"]
    failed = [run for run in runs if run.status != "ok"]
    current_stats = snapshot_stats(families)
    newest = max((family_activity(item) for item in families), default=datetime.min.replace(tzinfo=UTC))
    latest_sensor = max((timestamp(run.fetched_at) for run in runs), default=generated_at)
    ok = bool(successful) and len(successful) >= max(1, len(runs) // 2) and bool(families)
    total = len(runs)
    fresh = len(successful)
    return {
        "ok": ok,
        "read_only": True,
        "running": False,
        "stale": False,
        "generated_at": generated_at.isoformat(),
        "progress": {"mode": "v4-market-first", "stage": "complete", "completed": fresh, "total": total},
        "inventory": {
            "total": total,
            "fresh": fresh,
            "unhealthy": len(failed),
            "running": 0,
            "latest_activity_at": _iso(latest_sensor),
            "coverage_watermark": _iso(min((timestamp(run.fetched_at) for run in successful), default=generated_at)),
            "fresh_percent": round(100 * fresh / total, 1) if total else 0.0,
            "healthy": not failed and bool(successful),
            "kind": "market-sensors",
        },
        "market": {
            "sensor_total": total,
            "sensor_current": fresh,
            "sensor_failed": len(failed),
            "freshest_market_event_at": _iso(newest) if families else None,
            "new_verified_24h": current_stats.get("new_verified_24h", 0),
            "market_events_24h": current_stats.get("market_events_24h", 0),
            "verification_backlog": current_stats.get("verification_backlog", 0),
            "verification": verify_summary,
            "sensors": [run.__dict__ for run in runs],
        },
    }


def _validate(families: list[dict[str, object]], runs: list[SensorRun]) -> None:
    minimum = max(1, int(os.getenv("GAIA_V4_MIN_FAMILIES", "100")))
    if len(families) < minimum:
        raise RuntimeError(f"v4 snapshot has only {len(families)} families; minimum={minimum}")
    successful = sum(run.status == "ok" for run in runs)
    min_sensors = max(1, int(os.getenv("GAIA_V4_MIN_SENSORS", "3")))
    if successful < min_sensors:
        raise RuntimeError(f"only {successful} market sensors succeeded; minimum={min_sensors}")
    newest = max((family_activity(item) for item in families), default=datetime.min.replace(tzinfo=UTC))
    max_age = timedelta(hours=max(1, int(os.getenv("GAIA_V4_MAX_MARKET_AGE_HOURS", "96"))))
    if newest < datetime.now(UTC) - max_age:
        raise RuntimeError(f"freshest market event is stale: {newest.isoformat()}")


async def run(
    *,
    previous_path: Path,
    output_path: Path,
    sensor_concurrency: int,
    verify_concurrency: int,
) -> dict[str, object]:
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    if not isinstance(previous, dict):
        previous = {}
    started_at = datetime.now(UTC)

    sensor_raw, sensor_runs = await fetch_all_sensors(concurrency=sensor_concurrency)
    sensor_postings = [posting for posting in sensor_raw if is_default_target(posting)]
    sensor_by_url: dict[str, list[Posting]] = defaultdict(list)
    for posting in sensor_postings:
        sensor_by_url[posting.canonical_apply_url].append(posting)

    collectors = _verification_collectors(sensor_postings, previous)
    direct_raw, refreshed_direct, verify_summary = await _collect(
        collectors,
        concurrency=verify_concurrency,
    )
    direct_postings: list[Posting] = []
    for posting in direct_raw:
        candidate = _promote_direct(posting, sensor_by_url)
        if is_default_target(candidate):
            direct_postings.append(candidate)

    now = datetime.now(UTC)
    first_seen = _previous_first_seen(previous)
    previous_records = _previous_observations(previous, now)
    successful_sensor_sources = {f"sensor:{run.name}" for run in sensor_runs if run.status == "ok"}
    refreshed_sources = successful_sensor_sources | refreshed_direct
    preserved = [record for record in previous_records if record.posting.source not in refreshed_sources]
    incoming = _incoming_observations(
        [*sensor_postings, *direct_postings],
        first_seen_by_url=first_seen,
        now=now,
    )
    families = _build_families([*preserved, *incoming])
    _validate(families, sensor_runs)

    generated_at = datetime.now(UTC)
    health = _health(
        runs=sensor_runs,
        families=families,
        verify_summary=verify_summary,
        generated_at=generated_at,
    )
    payload: dict[str, object] = {
        "schema_version": 4,
        "generated_at": generated_at.isoformat(),
        "source_activity_at": health["inventory"]["latest_activity_at"],  # type: ignore[index]
        "max_stale_seconds": 1_800,
        "family_index": families,
        "family_index_total": len(families),
        "family_index_complete": True,
        "responses": snapshot_responses(families, health),
        "v4": {
            "objective": "minimize relevant market detection latency, then verify employer URLs",
            "started_at": started_at.isoformat(),
            "sensor_postings": len(sensor_postings),
            "sensor_unique_urls": len(sensor_by_url),
            "verification_collectors": len(collectors),
            "direct_postings": len(direct_postings),
            "refreshed_direct_sources": len(refreshed_direct),
            "stats": snapshot_stats(families),
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")
    temporary.replace(output_path)

    return {
        "families": len(families),
        "sensors_ok": sum(run.status == "ok" for run in sensor_runs),
        "sensors_total": len(sensor_runs),
        "sensor_postings": len(sensor_postings),
        "direct_postings": len(direct_postings),
        "verification_collectors": len(collectors),
        "stats": snapshot_stats(families),
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build GAIA v4 from low-latency market sensors then employer verification")
    parser.add_argument("--previous", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--sensor-concurrency", type=int, default=int(os.getenv("GAIA_V4_SENSOR_CONCURRENCY", "8")))
    parser.add_argument("--verify-concurrency", type=int, default=int(os.getenv("GAIA_V4_VERIFY_CONCURRENCY", "48")))
    args = parser.parse_args()
    summary = asyncio.run(
        run(
            previous_path=args.previous,
            output_path=args.output,
            sensor_concurrency=max(1, args.sensor_concurrency),
            verify_concurrency=max(1, args.verify_concurrency),
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
