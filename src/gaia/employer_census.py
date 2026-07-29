from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from psycopg.types.json import Jsonb

from .db import Database
from .discovery import collectors_from_registry
from .models import Posting
from .provider_discovery import provider_collectors_from_postings
from .quality import canonical_company
from .source_catalog import _spec, merge_catalog, save_candidates
from .universe import _employer_key

LOGGER = logging.getLogger("gaia.employer-census")

ECOSYSTEM_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS employer_observations (
        observation_key TEXT PRIMARY KEY,
        canonical_name TEXT NOT NULL,
        aliases TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        evidence_type TEXT NOT NULL,
        source TEXT NOT NULL,
        profile_url TEXT,
        official_url TEXT,
        location TEXT,
        sectors TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        internship_signal DOUBLE PRECISION NOT NULL DEFAULT 0
            CHECK (internship_signal BETWEEN 0 AND 1),
        technical_signal DOUBLE PRECISION NOT NULL DEFAULT 0
            CHECK (technical_signal BETWEEN 0 AND 1),
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        next_probe_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        last_probe_at TIMESTAMPTZ,
        probe_status TEXT NOT NULL DEFAULT 'candidate'
            CHECK (probe_status IN ('candidate','retry','resolved','unresolved')),
        consecutive_failures INTEGER NOT NULL DEFAULT 0
            CHECK (consecutive_failures >= 0),
        last_error TEXT,
        metadata JSONB NOT NULL DEFAULT '{}'::JSONB
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_employer_observations_due
        ON employer_observations (probe_status, next_probe_at, technical_signal DESC)
        WHERE probe_status IN ('candidate','retry','unresolved')
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_employer_observations_name
        ON employer_observations (lower(canonical_name))
    """,
)

EXCLUDED_PROFILE_HOSTS = {
    "ycombinator.com",
    "www.ycombinator.com",
    "linkedin.com",
    "www.linkedin.com",
    "github.com",
    "www.github.com",
    "twitter.com",
    "www.twitter.com",
    "x.com",
    "www.x.com",
    "facebook.com",
    "www.facebook.com",
    "crunchbase.com",
    "www.crunchbase.com",
    "youtube.com",
    "www.youtube.com",
}
CAREER_MARKERS = (
    "career",
    "careers",
    "job",
    "jobs",
    "join-us",
    "join_us",
    "work-with-us",
    "open-positions",
    "open-roles",
)
RESOLVABLE_KINDS = {
    "greenhouse",
    "lever",
    "ashby",
    "workday-search",
    "smartrecruiters",
    "recruitee",
    "workable",
    "jobvite",
    "icims",
    "oracle-cloud",
    "successfactors",
    "domain",
}


def ensure_ecosystem_schema(database: Database) -> None:
    with database.connect() as connection:
        for statement in ECOSYSTEM_SCHEMA_STATEMENTS:
            connection.execute(statement)


def _observation_key(source: str, profile_url: str, name: str) -> str:
    raw = f"{source}|{profile_url}|{name.casefold()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:28]


def _yc_company_name(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    batch = re.search(r"\b[WSFP]\d{4}\b", cleaned)
    if batch:
        cleaned = cleaned[: batch.start()].strip()
    cleaned = cleaned.split(" Active", 1)[0].split(" Inactive", 1)[0].strip()
    return cleaned


def _yc_observations(body: str, *, url: str, source: str, sectors: list[str]) -> list[dict[str, object]]:
    soup = BeautifulSoup(body, "html.parser")
    output: dict[str, dict[str, object]] = {}
    for anchor in soup.find_all("a", href=True):
        profile_url = urljoin(url, str(anchor.get("href")))
        parts = urlsplit(profile_url)
        segments = [segment for segment in parts.path.split("/") if segment]
        if parts.netloc not in {"www.ycombinator.com", "ycombinator.com"}:
            continue
        if len(segments) != 2 or segments[0] != "companies":
            continue
        name = _yc_company_name(anchor.get_text(" ", strip=True))
        if not name or name.casefold() in {"companies", "startup directory"}:
            continue
        output[profile_url] = {
            "name": name,
            "profile_url": profile_url,
            "location": "",
            "sectors": sectors,
            "metadata": {"directory_url": url, "slug": segments[1]},
        }
    return list(output.values())


def _upsert_observations(
    database: Database,
    *,
    source: str,
    evidence_type: str,
    internship_signal: float,
    technical_signal: float,
    observations: list[dict[str, object]],
) -> int:
    rows = []
    for item in observations:
        name = canonical_company(str(item.get("name") or ""))
        profile_url = str(item.get("profile_url") or "")
        if not name or not profile_url:
            continue
        rows.append(
            (
                _observation_key(source, profile_url, name),
                name,
                [name],
                evidence_type,
                source,
                profile_url,
                str(item.get("location") or "") or None,
                sorted({str(value) for value in item.get("sectors") or [] if value}),
                max(0.0, min(1.0, internship_signal)),
                max(0.0, min(1.0, technical_signal)),
                Jsonb(dict(item.get("metadata") or {})),
            )
        )
    if not rows:
        return 0
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO employer_observations(
                observation_key, canonical_name, aliases, evidence_type, source,
                profile_url, location, sectors, internship_signal, technical_signal,
                metadata
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(observation_key) DO UPDATE SET
                canonical_name=excluded.canonical_name,
                aliases=ARRAY(
                    SELECT DISTINCT value
                    FROM unnest(employer_observations.aliases || excluded.aliases) AS value
                    ORDER BY value
                ),
                location=COALESCE(excluded.location, employer_observations.location),
                sectors=ARRAY(
                    SELECT DISTINCT value
                    FROM unnest(employer_observations.sectors || excluded.sectors) AS value
                    ORDER BY value
                ),
                internship_signal=GREATEST(
                    employer_observations.internship_signal,
                    excluded.internship_signal
                ),
                technical_signal=GREATEST(
                    employer_observations.technical_signal,
                    excluded.technical_signal
                ),
                last_seen_at=now(),
                metadata=employer_observations.metadata || excluded.metadata,
                probe_status=CASE
                    WHEN employer_observations.probe_status='resolved' THEN 'resolved'
                    ELSE 'candidate'
                END
            """,
            rows,
        )
    return len(rows)


def _claim_observations(
    database: Database,
    *,
    worker_id: str,
    limit: int,
    lease_seconds: int,
) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            WITH selected AS (
                SELECT observation_key
                FROM employer_observations
                WHERE probe_status IN ('candidate','retry','unresolved')
                  AND next_probe_at<=now()
                  AND (lease_expires_at IS NULL OR lease_expires_at<now())
                ORDER BY
                    (official_url IS NOT NULL) DESC,
                    technical_signal DESC,
                    internship_signal DESC,
                    consecutive_failures,
                    last_seen_at DESC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE employer_observations AS observation
            SET lease_owner=%s,
                lease_expires_at=now() + (%s * interval '1 second'),
                last_probe_at=now()
            FROM selected
            WHERE observation.observation_key=selected.observation_key
            RETURNING observation.*
            """,
            (limit, worker_id, lease_seconds),
        ).fetchall()
    return [dict(row) for row in rows]


def _external_links(body: str, base_url: str) -> list[tuple[int, str]]:
    soup = BeautifulSoup(body, "html.parser")
    links: dict[str, int] = {}
    base_host = urlsplit(base_url).netloc.casefold()
    for anchor in soup.find_all("a", href=True):
        url = urljoin(base_url, str(anchor.get("href")))
        parts = urlsplit(url)
        host = parts.netloc.casefold()
        if parts.scheme not in {"http", "https"} or not host:
            continue
        text = anchor.get_text(" ", strip=True).casefold()
        score = 0
        if host != base_host:
            score += 20
        if "website" in text or "homepage" in text or "company site" in text:
            score += 100
        if any(marker in text or marker in parts.path.casefold() for marker in CAREER_MARKERS):
            score += 80
        links[url] = max(links.get(url, -1), score)
    return sorted(((score, url) for url, score in links.items()), reverse=True)


def _official_url(profile_body: str, profile_url: str) -> str | None:
    for _, url in _external_links(profile_body, profile_url):
        host = urlsplit(url).netloc.casefold().split(":", 1)[0]
        if host not in EXCLUDED_PROFILE_HOSTS:
            return url
    return None


def _career_links(body: str, base_url: str, *, same_host_only: bool) -> list[str]:
    base_host = urlsplit(base_url).netloc.casefold()
    output: list[str] = []
    for score, url in _external_links(body, base_url):
        parts = urlsplit(url)
        if same_host_only and parts.netloc.casefold() != base_host:
            continue
        text = f"{parts.path} {parts.query}".casefold()
        if score >= 80 or any(marker in text for marker in CAREER_MARKERS):
            output.append(url)
    return list(dict.fromkeys(output))


async def _fetch_text(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    response = await client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").casefold()
    if "html" not in content_type and "text" not in content_type:
        return str(response.url), ""
    return str(response.url), response.text


async def _resolve_observation(
    client: httpx.AsyncClient,
    database: Database,
    settings: dict[str, object],
    row: Mapping[str, object],
    *,
    worker_id: str,
) -> int:
    key = str(row["observation_key"])
    name = str(row["canonical_name"])
    profile_url = str(row.get("profile_url") or "")
    official_url = str(row.get("official_url") or "") or None
    try:
        profile_body = ""
        if profile_url:
            _, profile_body = await _fetch_text(client, profile_url)
        if official_url is None and profile_body:
            official_url = _official_url(profile_body, profile_url)
        if official_url is None:
            raise ValueError("official company website not found")

        official_url, homepage = await _fetch_text(client, official_url)
        candidate_urls = [official_url]
        candidate_urls.extend(_career_links(homepage, official_url, same_host_only=True)[:6])

        if profile_body:
            yc_jobs = [
                url
                for _, url in _external_links(profile_body, profile_url)
                if urlsplit(url).netloc.casefold() in {"ycombinator.com", "www.ycombinator.com"}
                and "/jobs" in urlsplit(url).path.casefold()
            ]
            for jobs_url in yc_jobs[:1]:
                try:
                    final_url, jobs_body = await _fetch_text(client, jobs_url)
                except httpx.HTTPError:
                    continue
                candidate_urls.extend(
                    url
                    for _, url in _external_links(jobs_body, final_url)
                    if urlsplit(url).netloc.casefold()
                    not in EXCLUDED_PROFILE_HOSTS
                )

        postings = [
            Posting(
                company=name,
                title="Employer careers surface",
                apply_url=url,
                source=f"ecosystem:{row['source']}",
                source_id=url,
                source_mode="ecosystem-observation",
            )
            for url in dict.fromkeys(candidate_urls)
        ]
        generated = merge_catalog(
            collectors_from_registry(postings, settings, deep=True),
            provider_collectors_from_postings(postings),
        )
        collectors = []
        for collector in generated:
            described = _spec(collector)
            if described is None or described[0] not in RESOLVABLE_KINDS:
                continue
            collector.scope = "current"
            collectors.append(collector)
        saved = save_candidates(database, collectors, origin="employer-ecosystem")

        with database.connect() as connection:
            connection.execute(
                """
                UPDATE employer_observations
                SET official_url=%s,
                    probe_status=%s,
                    next_probe_at=now() + (%s * interval '1 second'),
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    consecutive_failures=0,
                    last_error=NULL,
                    metadata=metadata || %s
                WHERE observation_key=%s AND lease_owner=%s
                """,
                (
                    official_url,
                    "resolved" if saved else "unresolved",
                    30 * 86400 if saved else 7 * 86400,
                    Jsonb({"candidate_surfaces": saved}),
                    key,
                    worker_id,
                ),
            )
        return saved
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        failures = int(row.get("consecutive_failures") or 0) + 1
        delay = min(30 * 86400, 12 * 3600 * (2 ** min(failures - 1, 5)))
        with database.connect() as connection:
            connection.execute(
                """
                UPDATE employer_observations
                SET probe_status='retry',
                    next_probe_at=now() + (%s * interval '1 second'),
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    consecutive_failures=%s,
                    last_error=%s
                WHERE observation_key=%s AND lease_owner=%s
                """,
                (delay, failures, repr(exc), key, worker_id),
            )
        return 0


async def refresh_employer_ecosystems(
    client: httpx.AsyncClient,
    database: Database,
    settings: dict[str, object],
    *,
    refresh_feeds: bool,
    worker_id: str,
    lease_seconds: int,
) -> dict[str, int]:
    """Refresh independent employer evidence and resolve official hiring surfaces."""

    ensure_ecosystem_schema(database)
    observed = 0
    if refresh_feeds:
        for raw in settings.get("employer_ecosystems", []):
            item = dict(raw)
            kind = str(item.get("kind") or "")
            url = str(item.get("url") or "")
            if kind != "yc-directory" or not url:
                continue
            source = str(item.get("name") or url)
            try:
                response = await client.get(url)
                response.raise_for_status()
                observations = _yc_observations(
                    response.text,
                    url=url,
                    source=source,
                    sectors=[str(value) for value in item.get("sectors") or []],
                )
                observed += _upsert_observations(
                    database,
                    source=f"yc:{source}",
                    evidence_type="startup-ecosystem",
                    internship_signal=float(item.get("internship_signal") or 0.32),
                    technical_signal=float(item.get("technical_signal") or 0.86),
                    observations=observations,
                )
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                LOGGER.warning("employer ecosystem feed failed: %s: %r", source, exc)

    limit = max(1, int(os.getenv("GAIA_EMPLOYER_PROFILE_PROBE_LIMIT", "24")))
    claimed = _claim_observations(
        database,
        worker_id=worker_id,
        limit=limit,
        lease_seconds=lease_seconds,
    )
    resolved = 0
    if claimed:
        resolved = sum(
            await asyncio.gather(
                *(
                    _resolve_observation(
                        client,
                        database,
                        settings,
                        row,
                        worker_id=worker_id,
                    )
                    for row in claimed
                )
            )
        )
    return {"observed": observed, "probed": len(claimed), "candidate_surfaces": resolved}


def merge_observations_into_universe(database: Database) -> dict[str, int]:
    """Bulk-merge ecosystem employers into the evidence-backed employer census."""

    ensure_ecosystem_schema(database)
    now = datetime.now(UTC)
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT
                observation_key,
                canonical_name,
                aliases,
                evidence_type,
                source,
                profile_url,
                official_url,
                internship_signal,
                technical_signal,
                first_seen_at,
                last_seen_at,
                metadata
            FROM employer_observations
            ORDER BY lower(canonical_name), source, observation_key
            """
        ).fetchall()
        if not rows:
            return {"observations": 0, "merged": 0, "inserted": 0}

        existing_keys = {
            str(row["employer_key"])
            for row in connection.execute(
                "SELECT employer_key FROM employer_universe"
            ).fetchall()
        }
        employers: dict[str, dict[str, object]] = {}
        evidence: dict[str, dict[str, object]] = {}

        for row in rows:
            raw_name = str(row["canonical_name"]).strip()
            name = canonical_company(raw_name) or raw_name
            key = _employer_key(name)
            recency_days = max(
                0.0,
                (now - row["last_seen_at"]).total_seconds() / 86400,
            )
            recency = math.exp(-recency_days / 730)
            internship = float(row["internship_signal"] or 0)
            technical = float(row["technical_signal"] or 0)
            score = round(
                100 * (0.42 * internship + 0.40 * technical + 0.18 * recency),
                3,
            )

            item = employers.setdefault(
                key,
                {
                    "name": name,
                    "aliases": set(),
                    "evidence_types": set(),
                    "sources": set(),
                    "count": 0,
                    "located": False,
                    "internship": 0.0,
                    "technical": 0.0,
                    "score": 0.0,
                    "first_seen": row["first_seen_at"],
                    "last_seen": row["last_seen_at"],
                },
            )
            aliases = item["aliases"]
            evidence_types = item["evidence_types"]
            sources = item["sources"]
            assert isinstance(aliases, set)
            assert isinstance(evidence_types, set)
            assert isinstance(sources, set)
            aliases.update(str(alias) for alias in (row["aliases"] or [name]) if alias)
            evidence_types.add(str(row["evidence_type"]))
            sources.add(str(row["source"]))
            item["count"] = int(item["count"]) + 1
            item["located"] = bool(item["located"] or row["official_url"])
            item["internship"] = max(float(item["internship"]), internship)
            item["technical"] = max(float(item["technical"]), technical)
            item["score"] = max(float(item["score"]), score)
            item["first_seen"] = min(item["first_seen"], row["first_seen_at"])
            item["last_seen"] = max(item["last_seen"], row["last_seen_at"])

            evidence_type = str(row["evidence_type"])
            source_name = str(row["source"])
            evidence_key = hashlib.sha256(
                f"{key}|{evidence_type}|{source_name}|ecosystem".encode()
            ).hexdigest()[:28]
            evidence_item = evidence.setdefault(
                evidence_key,
                {
                    "employer_key": key,
                    "evidence_type": evidence_type,
                    "source": source_name,
                    "first_seen": row["first_seen_at"],
                    "last_seen": row["last_seen_at"],
                    "sample_url": row["official_url"] or row["profile_url"],
                    "metadata": {},
                },
            )
            evidence_item["first_seen"] = min(
                evidence_item["first_seen"], row["first_seen_at"]
            )
            if row["last_seen_at"] >= evidence_item["last_seen"]:
                evidence_item["last_seen"] = row["last_seen_at"]
                evidence_item["sample_url"] = row["official_url"] or row["profile_url"]
            metadata = evidence_item["metadata"]
            assert isinstance(metadata, dict)
            metadata.update(dict(row["metadata"] or {}))

        universe_rows = []
        for key, item in employers.items():
            aliases = item["aliases"]
            evidence_types = item["evidence_types"]
            sources = item["sources"]
            assert isinstance(aliases, set)
            assert isinstance(evidence_types, set)
            assert isinstance(sources, set)
            technical = float(item["technical"])
            universe_rows.append(
                (
                    key,
                    item["name"],
                    sorted(aliases),
                    "located" if item["located"] else "candidate",
                    int(item["count"]),
                    sorted(evidence_types),
                    sorted(sources),
                    float(item["internship"]),
                    technical,
                    float(item["score"]),
                    technical >= 0.5,
                    item["first_seen"],
                    item["last_seen"],
                    now,
                )
            )

        connection.executemany(
            """
            INSERT INTO employer_universe(
                employer_key, canonical_name, aliases, resolution_status,
                evidence_count, evidence_types, evidence_sources,
                historical_years, historical_internships,
                current_index_mentions, direct_sources, direct_openings,
                technical_roles, internship_probability,
                technical_probability, frontier_score, blind_spot,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,ARRAY[]::SMALLINT[],0,0,0,0,0,
                %s,%s,%s,%s,%s,%s,%s
            )
            ON CONFLICT(employer_key) DO UPDATE SET
                aliases=ARRAY(
                    SELECT DISTINCT value
                    FROM unnest(employer_universe.aliases || excluded.aliases) AS value
                    ORDER BY value
                ),
                resolution_status=CASE
                    WHEN excluded.resolution_status='located' THEN 'located'
                    ELSE employer_universe.resolution_status
                END,
                evidence_count=employer_universe.evidence_count + excluded.evidence_count,
                evidence_types=ARRAY(
                    SELECT DISTINCT value
                    FROM unnest(
                        employer_universe.evidence_types || excluded.evidence_types
                    ) AS value
                    ORDER BY value
                ),
                evidence_sources=ARRAY(
                    SELECT DISTINCT value
                    FROM unnest(
                        employer_universe.evidence_sources || excluded.evidence_sources
                    ) AS value
                    ORDER BY value
                ),
                internship_probability=GREATEST(
                    employer_universe.internship_probability,
                    excluded.internship_probability
                ),
                technical_probability=GREATEST(
                    employer_universe.technical_probability,
                    excluded.technical_probability
                ),
                frontier_score=GREATEST(
                    employer_universe.frontier_score,
                    excluded.frontier_score
                ),
                blind_spot=(
                    employer_universe.current_index_mentions=0
                    AND GREATEST(
                        employer_universe.technical_probability,
                        excluded.technical_probability
                    ) >= 0.5
                ),
                first_seen_at=LEAST(
                    employer_universe.first_seen_at,
                    excluded.first_seen_at
                ),
                last_seen_at=GREATEST(
                    employer_universe.last_seen_at,
                    excluded.last_seen_at
                ),
                updated_at=now()
            WHERE employer_universe.resolution_status!='enumerated'
            """,
            universe_rows,
        )

        evidence_rows = [
            (
                evidence_key,
                item["employer_key"],
                item["evidence_type"],
                item["source"],
                item["first_seen"],
                item["last_seen"],
                item["sample_url"],
                Jsonb(item["metadata"]),
            )
            for evidence_key, item in evidence.items()
        ]
        connection.executemany(
            """
            INSERT INTO employer_evidence(
                evidence_key, employer_key, evidence_type, source, event_year,
                role_count, active_roles, first_seen_at, last_seen_at,
                sample_url, metadata
            ) VALUES (%s,%s,%s,%s,NULL,0,0,%s,%s,%s,%s)
            ON CONFLICT(evidence_key) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                sample_url=excluded.sample_url,
                metadata=excluded.metadata
            """,
            evidence_rows,
        )

    inserted = sum(key not in existing_keys for key in employers)
    return {
        "observations": len(rows),
        "merged": len(rows) - inserted,
        "inserted": inserted,
    }
