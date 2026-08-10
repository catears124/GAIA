from __future__ import annotations

import asyncio
import gzip
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

import httpx

from .classify import classify
from .models import Posting
from .v4_sensors import SensorRun, fetch_all_sensors

OPENROLES_MANIFEST_URL = "https://openroles.today/data/manifest.json"


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decode_chunk(content: bytes) -> list[dict[str, object]]:
    raw = gzip.decompress(content) if content.startswith(b"\x1f\x8b") else content
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("OpenRoles slim chunk is not a JSON array")
    return [row for row in payload if isinstance(row, dict)]


def _candidate_chunks(
    manifest: dict[str, object],
    *,
    cutoff: datetime,
    limit: int,
) -> list[dict[str, object]]:
    chunks = manifest.get("slim_index_chunks")
    if not isinstance(chunks, list):
        raise ValueError("OpenRoles manifest is missing slim_index_chunks")
    selected: list[dict[str, object]] = []
    for raw in chunks:
        if not isinstance(raw, dict) or not raw.get("file"):
            continue
        posted_max = _timestamp(raw.get("posted_max"))
        if posted_max is not None and posted_max < cutoff:
            # The index is newest-first, so once a dated chunk is wholly older
            # than our window the remaining dated chunks are colder still.
            break
        selected.append(raw)
        if len(selected) >= limit:
            break
    return selected


def _rows_to_postings(
    rows: list[dict[str, object]],
    *,
    fetched_at: datetime,
    cutoff: datetime,
) -> list[Posting]:
    postings: list[Posting] = []
    for row in rows:
        if int(row.get("s") or 0) == 1 or int(row.get("r") or 0) == 1:
            continue
        company = str(row.get("c") or "").strip()
        title = str(row.get("ti") or "").strip()
        url = str(row.get("u") or "").strip()
        if not company or not title or not url:
            continue

        posted = _timestamp(row.get("p"))
        first_seen = _timestamp(row.get("f"))
        signal_at = posted or first_seen
        if signal_at is None or signal_at < cutoff:
            continue

        location = str(row.get("loc") or "").strip()
        posting = Posting(
            company=company,
            title=title,
            apply_url=url,
            source="sensor:openroles-recent",
            source_id=str(row.get("i") or url),
            locations=[location] if location else [],
            source_mode="market-sensor",
            # OpenRoles obtained this timestamp from an ATS, but GAIA has not yet
            # independently checked that employer surface. Keep it on the sensor
            # axis until the verifier reaches the direct URL itself.
            sensor_reported_at=signal_at,
            sensor_reported_raw=str(row.get("p") or row.get("f") or "") or None,
            sensor_precision="timestamp",
            sensor_confidence="source-reported",
            observed_at=fetched_at,
        )
        postings.append(classify(posting, source_confirms_2027=False))
    return postings


async def fetch_openroles_recent(
    client: httpx.AsyncClient,
    *,
    fetched_at: datetime | None = None,
    days: int | None = None,
    chunk_limit: int | None = None,
) -> tuple[list[Posting], SensorRun]:
    fetched_at = fetched_at or datetime.now(UTC)
    days = max(1, days if days is not None else int(os.getenv("GAIA_V4_OPENROLES_DAYS", "7")))
    chunk_limit = max(
        1,
        chunk_limit if chunk_limit is not None else int(os.getenv("GAIA_V4_OPENROLES_CHUNKS", "6")),
    )
    cutoff = fetched_at - timedelta(days=days)
    manifest_url = os.getenv("GAIA_V4_OPENROLES_MANIFEST", OPENROLES_MANIFEST_URL)

    try:
        manifest_response = await client.get(manifest_url)
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        if not isinstance(manifest, dict):
            raise ValueError("OpenRoles manifest is not a JSON object")
        chunks = _candidate_chunks(manifest, cutoff=cutoff, limit=chunk_limit)
        if not chunks:
            raise ValueError("OpenRoles manifest has no recent slim-index chunks")

        async def fetch_chunk(chunk: dict[str, object]) -> list[dict[str, object]]:
            chunk_url = urljoin(manifest_url, str(chunk["file"]))
            response = await client.get(chunk_url)
            response.raise_for_status()
            return _decode_chunk(response.content)

        batches = await asyncio.gather(*(fetch_chunk(chunk) for chunk in chunks))
        rows = [row for batch in batches for row in batch]
        postings = _rows_to_postings(rows, fetched_at=fetched_at, cutoff=cutoff)
        return postings, SensorRun(
            "openroles-recent",
            manifest_url,
            "ok",
            len(rows),
            len(postings),
            fetched_at.isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - one external discovery sensor must not halt GAIA
        return [], SensorRun(
            "openroles-recent",
            manifest_url,
            "failed",
            0,
            0,
            fetched_at.isoformat(),
            repr(exc)[:500],
        )


async def fetch_all_market_sensors(
    *,
    concurrency: int = 8,
    timeout_seconds: float = 30.0,
) -> tuple[list[Posting], list[SensorRun]]:
    """Fetch GAIA's tracker sensors plus a bounded slice of OpenRoles' fresh ATS corpus."""
    headers = {"User-Agent": "GAIA/7.0 market-sensor-wave (+https://github.com/catears124/GAIA)"}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=True,
        headers=headers,
        limits=httpx.Limits(max_connections=max(16, concurrency * 2)),
    ) as client:
        base_task = asyncio.create_task(
            fetch_all_sensors(concurrency=concurrency, timeout_seconds=timeout_seconds)
        )
        openroles_task = asyncio.create_task(fetch_openroles_recent(client))
        (base_postings, base_runs), (openroles_postings, openroles_run) = await asyncio.gather(
            base_task,
            openroles_task,
        )
    return [*base_postings, *openroles_postings], [*base_runs, openroles_run]
