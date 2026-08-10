from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .collectors import Collector, SchemaPageCollector
from .discovery import collectors_from_registry
from .models import Posting

FAST_BOARD_PREFIXES = ("greenhouse:", "lever:", "ashby:")
DIRECT_BOARD_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
)

# These domains are useful market sensors, but loading a job detail page on them is
# NOT independent employer verification. They can introduce or corroborate a Lead;
# they can never be the observation that turns the badge green.
AGGREGATOR_HOST_SUFFIXES = (
    "simplify.jobs",
    "speedyapply.com",
    "openroles-ai.vercel.app",
    "jobright.ai",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "handshake.com",
    "wellfound.com",
    "levels.fyi",
)


def _stable_key(value: str) -> int:
    return int.from_bytes(hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest(), "big")


def _posted_priority(posting: Posting) -> tuple[int, float, int]:
    """Prefer explicit employer/source timestamps; undated rows rotate separately."""
    timestamp = posting.posted_at or posting.sensor_reported_at
    if timestamp is not None:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return 2, timestamp.timestamp(), _stable_key(posting.canonical_apply_url)
    return 1, 0.0, _stable_key(posting.canonical_apply_url)


def _round_robin(items: list[Posting], limit: int, *, slot: int) -> list[Posting]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return items
    ordered = sorted(items, key=lambda item: _stable_key(item.canonical_apply_url))
    start = (slot * limit) % len(ordered)
    return (ordered[start:] + ordered[:start])[:limit]


def _rotate_collectors(collectors: list[Collector], budget: int, *, slot: int) -> list[Collector]:
    if budget <= 0 or not collectors:
        return []
    ordered = sorted(collectors, key=lambda collector: _stable_key(collector.name))
    if len(ordered) <= budget:
        return ordered
    start = (slot * budget) % len(ordered)
    return (ordered[start:] + ordered[:start])[:budget]


def _aggregator_host(host: str) -> bool:
    host = host.casefold().strip(".")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in AGGREGATOR_HOST_SUFFIXES)


def _hot_page_collectors(
    postings: list[Posting],
    *,
    limit: int,
    batch_size: int,
    slot: int,
) -> list[Collector]:
    """Verify exact employer/ATS URLs for hosts without a cheap enumerable API.

    This includes Workday. Workday board enumeration is intentionally rate-limited,
    but a newly detected Workday job should not wait hours for its tenant's turn in
    the board sweep before GAIA can validate the exact public application page.

    Aggregator detail pages are explicitly excluded. Their content is evidence that
    a role exists in the market, not independent employer evidence.
    """
    candidates: dict[str, Posting] = {}
    for posting in postings:
        host = urlsplit(posting.apply_url).netloc.casefold()
        if not host:
            continue
        if any(fragment in host for fragment in DIRECT_BOARD_HOSTS):
            continue
        if _aggregator_host(host):
            continue
        existing = candidates.get(posting.canonical_apply_url)
        if existing is None or _posted_priority(posting) > _posted_priority(existing):
            candidates[posting.canonical_apply_url] = posting

    dated = [posting for posting in candidates.values() if posting.posted_at or posting.sensor_reported_at]
    undated = [posting for posting in candidates.values() if not (posting.posted_at or posting.sensor_reported_at)]
    dated.sort(key=_posted_priority, reverse=True)
    chosen = dated[:limit]
    remaining = max(0, limit - len(chosen))
    if remaining:
        chosen.extend(_round_robin(undated, remaining, slot=slot))

    grouped: dict[tuple[str, str], list[Posting]] = defaultdict(list)
    for posting in chosen:
        host = urlsplit(posting.apply_url).netloc.casefold()
        grouped[(posting.company, host)].append(posting)

    collectors: list[Collector] = []
    batch_size = max(1, batch_size)
    for (company, host), leads in sorted(grouped.items(), key=lambda item: (item[0][1], item[0][0].casefold())):
        leads.sort(key=_posted_priority, reverse=True)
        for offset in range(0, len(leads), batch_size):
            batch = leads[offset : offset + batch_size]
            suffix = f":{offset // batch_size + 1}" if len(leads) > batch_size else ""
            collectors.append(
                SchemaPageCollector(
                    company,
                    name=f"hot-page:{host}:{company}{suffix}",
                    leads=batch,
                    trusted=True,
                )
            )
    return collectors


def plan_verification_collectors(
    sensor_postings: list[Posting],
    *,
    durable_postings: list[Posting] | None = None,
    settings: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[Collector]:
    """Build a bounded verification wave optimized for latency, not source count.

    Lane 1: enumerate every cheap current Greenhouse/Lever/Ashby board we know.
    Lane 2: directly validate the hottest exact employer/ATS pages on other hosts.
    Lane 3: rotate a small Workday board budget for durable enumeration/closure.

    The direct-page lane means a fresh role can become verified this wave even when
    its ATS-wide crawler is deliberately rate-limited. The rotation lane provides
    eventual full coverage without allowing Workday's one-at-a-time pacing to hold
    the whole publication pipeline hostage.
    """
    now = now or datetime.now(UTC)
    slot = int(now.timestamp() // max(60, int(os.getenv("GAIA_V4_VERIFY_ROTATION_SECONDS", "900"))))
    durable_postings = durable_postings or []

    current_base = collectors_from_registry(sensor_postings, settings=settings)
    durable_base = collectors_from_registry(durable_postings, settings=settings) if durable_postings else []

    cheap_boards: dict[str, Collector] = {}
    workday: dict[str, Collector] = {}
    for collector in [*current_base, *durable_base]:
        if collector.name.startswith(FAST_BOARD_PREFIXES):
            cheap_boards.setdefault(collector.name, collector)
        elif collector.name.startswith("workday:"):
            # Current market evidence wins over a durable copy of the same board.
            workday.setdefault(collector.name, collector)

    hot_limit = max(50, int(os.getenv("GAIA_V4_HOT_PAGE_LIMIT", "900")))
    hot_batch = max(5, int(os.getenv("GAIA_V4_HOT_PAGE_BATCH", "40")))
    hot_pages = _hot_page_collectors(
        sensor_postings,
        limit=hot_limit,
        batch_size=hot_batch,
        slot=slot,
    )

    workday_budget = max(0, int(os.getenv("GAIA_V4_WORKDAY_BOARD_BUDGET", "12")))
    workday_wave = _rotate_collectors(list(workday.values()), workday_budget, slot=slot)

    # Current cheap boards first, then exact hot pages, then intentionally slow
    # board rotation. Preserve stable uniqueness across all lanes.
    planned: list[Collector] = []
    seen: set[str] = set()
    for collector in [*cheap_boards.values(), *hot_pages, *workday_wave]:
        if collector.name in seen:
            continue
        seen.add(collector.name)
        planned.append(collector)

    hard_limit = max(100, int(os.getenv("GAIA_V4_VERIFY_COLLECTOR_LIMIT", "1400")))
    return planned[:hard_limit]
