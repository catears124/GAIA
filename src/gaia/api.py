from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .db import Database
from .quality import (
    TECH_CATEGORIES,
    canonical_company,
    is_index_mode,
    normalize_locations,
)
from .service import SyncService

FRONTEND = Path(__file__).with_name("frontend")
TARGET_MATCHES = ("exact", "year_confirmed", "source_confirmed")
db = Database()
service = SyncService(db, concurrency=int(os.getenv("GAIA_CONCURRENCY", "16")))


def _initial_sync_due() -> bool:
    if os.getenv("GAIA_INITIAL_SYNC", "1") != "1":
        return False
    freshness_hours = max(1.0, float(os.getenv("GAIA_INITIAL_SYNC_MAX_AGE_HOURS", "6")))
    with db.connect() as connection:
        row = connection.execute(
            """
            SELECT EXTRACT(EPOCH FROM (now() - MAX(finished_at))) / 3600.0 AS age_hours
            FROM sync_runs
            WHERE finished_at IS NOT NULL
            """
        ).fetchone()
    age = row["age_hours"]
    return age is None or float(age) >= freshness_hours


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if _initial_sync_due():
        service.start_background("refresh")
    yield
    await service.stop()


app = FastAPI(title="GAIA", version="4.0.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")


@app.get("/", include_in_schema=False)
def root(request: Request) -> HTMLResponse:
    content = (FRONTEND / "index.html").read_text(encoding="utf-8")
    og_image = request.url_for("assets", path="og.png")
    return HTMLResponse(content.replace("__GAIA_OG_IMAGE__", str(og_image)))


@app.get("/api/health")
def health() -> dict[str, object]:
    with db.connect() as connection:
        latest_run = connection.execute(
            """
            SELECT id, started_at, finished_at, status, sources, postings, failed
            FROM sync_runs
            WHERE finished_at IS NOT NULL AND status IN ('ok', 'partial')
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        latest_success = connection.execute(
            "SELECT MAX(finished_at) AS finished_at FROM sync_runs WHERE status='ok'"
        ).fetchone()["finished_at"]
        source_state = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status IN ('broken', 'truncated')) AS failing
            FROM source_health
            WHERE scope='current' AND last_run_id=%s
            """,
            (int(latest_run["id"]) if latest_run else -1,),
        ).fetchone()
    run = db._json_row(latest_run) if latest_run else None  # noqa: SLF001
    return {
        "ok": latest_run is not None,
        "read_only": os.getenv("GAIA_READ_ONLY", "0") == "1",
        "running": service.running,
        "progress": service.progress.as_dict(),
        "last_summary": service.last_summary.as_dict() if service.last_summary else None,
        "data": {
            "last_run": run,
            "last_success_at": db._json_row({"value": latest_success})["value"]  # noqa: SLF001
            if latest_success
            else None,
            "sources": int(source_state["total"] or 0),
            "failing_sources": int(source_state["failing"] or 0),
        },
    }


def _catalog_count() -> int:
    with db.connect() as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM source_catalog").fetchone()
    return int(row["count"])


def _target_clause(target: str, params: list[object]) -> str:
    if target == "default":
        params.append(list(TARGET_MATCHES))
        return "target_match = ANY(%s)"
    if target:
        params.append(target)
        return "target_match=%s"
    return "TRUE"


def _tech_clause(category: str, track: str, params: list[object]) -> str:
    if category:
        params.append(category)
        return "category=%s"
    if track != "all":
        params.append(list(TECH_CATEGORIES))
        return "category = ANY(%s)"
    return "TRUE"


def _trust_clause(trust: str) -> str:
    if trust == "all":
        return "TRUE"
    if trust == "leads":
        return "direct_openings=0 AND backstop_openings>0"
    return "direct_openings>0"


def _escape_like(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _search_clause(query: str, params: list[object]) -> str:
    clauses: list[str] = []
    for token in query.split():
        pattern = f"%{_escape_like(token)}%"
        clauses.append(
            "(company ILIKE %s ESCAPE '!' OR title ILIKE %s ESCAPE '!' "
            "OR array_to_string(locations, ' ') ILIKE %s ESCAPE '!')"
        )
        params.extend([pattern, pattern, pattern])
    return " AND ".join(clauses) or "TRUE"


def _location_clause(location: str, params: list[object]) -> str:
    if re.fullmatch(r"[A-Za-z]{2}", location):
        code = location.upper()
        params.extend([code, f"%, {code}"])
        return (
            "EXISTS (SELECT 1 FROM unnest(locations) AS value "
            "WHERE value = %s OR value ILIKE %s)"
        )
    params.append(f"%{_escape_like(location)}%")
    return (
        "EXISTS (SELECT 1 FROM unnest(locations) AS value "
        "WHERE value ILIKE %s ESCAPE '!')"
    )


def _search_order(query: str, sort: str, params: list[object]) -> str:
    if not query:
        return _order_clause(sort)
    phrase = _escape_like(query)
    first = query.split()[0]
    first_pattern = _escape_like(first)
    params.extend([query, first, f"{phrase}%", f"{first_pattern}%", f"%{phrase}%"])
    return (
        "CASE "
        "WHEN lower(company) = lower(%s) THEN 0 "
        "WHEN lower(company) = lower(%s) THEN 1 "
        "WHEN company ILIKE %s ESCAPE '!' THEN 2 "
        "WHEN company ILIKE %s ESCAPE '!' THEN 3 "
        "WHEN title ILIKE %s ESCAPE '!' THEN 4 "
        "ELSE 5 END, "
        f"{_order_clause(sort)}"
    )


def _order_clause(sort: str) -> str:
    if sort == "company":
        return "lower(company), lower(title), family_key"
    if sort == "verified":
        return "last_verified_at DESC, first_detected_at DESC, family_key"
    return (
        "COALESCE(latest_posted_at, first_detected_at) DESC, "
        "last_verified_at DESC, first_detected_at DESC, family_key"
    )


def _opening_is_lead(opening: dict[str, object]) -> bool:
    return is_index_mode(str(opening.get("source_mode") or ""))


def _opening_is_direct(opening: dict[str, object]) -> bool:
    return str(opening.get("source_mode") or "") == "direct"


def _present_family(row: Mapping[str, object], *, trust: str = "all") -> dict[str, object]:
    item = db._family_dict(row)  # noqa: SLF001 - central presentation normalization.
    item["company"] = canonical_company(str(item.get("company") or ""))
    item["locations"] = normalize_locations(item.get("locations") or [])
    cleaned_openings: list[dict[str, object]] = []
    for opening in item.get("openings") or []:
        if not isinstance(opening, dict):
            continue
        if trust == "verified" and not _opening_is_direct(opening):
            continue
        if trust == "leads" and not _opening_is_lead(opening):
            continue
        copy = dict(opening)
        copy["location"] = normalize_locations(copy.get("location") or [])
        cleaned_openings.append(copy)
    item["openings"] = cleaned_openings
    if trust in {"verified", "leads"}:
        item["opening_count"] = len(cleaned_openings)
    item["locations"] = normalize_locations(
        [location for opening in cleaned_openings for location in opening.get("location", [])]
        or item["locations"]
    )
    item["location_count"] = len(item["locations"])
    item["verified"] = int(item.get("direct_openings") or 0) > 0
    item["quality"] = "verified" if item["verified"] else "lead"
    return item


@app.get("/api/stats")
def stats() -> dict[str, int]:
    target_params: list[object] = []
    target_clause = _target_clause("default", target_params)
    tech_params: list[object] = []
    tech_clause = _tech_clause("", "tech", tech_params)
    params = [*target_params, *tech_params]
    with db.connect() as connection:
        row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS role_families,
                COALESCE(SUM(direct_openings), 0) AS active_listings,
                COUNT(DISTINCT company) AS companies,
                COALESCE(
                    SUM(direct_openings) FILTER (
                        WHERE latest_posted_at >= now() - interval '1 day'
                    ),
                    0
                ) AS new_24h,
                COUNT(*) FILTER (
                    WHERE latest_posted_at >= now() - interval '1 day'
                ) AS new_families_24h,
                COALESCE(SUM(direct_openings), 0) AS verified_listings,
                COUNT(*) AS verified_families
            FROM families
            WHERE {target_clause}
              AND {tech_clause}
              AND direct_openings>0
            """,
            params,
        ).fetchone()
        lead_row = connection.execute(
            f"""
            SELECT COUNT(*) AS leads, COALESCE(SUM(backstop_openings),0) AS lead_apps
            FROM families
            WHERE {target_clause}
              AND {tech_clause}
              AND direct_openings=0
              AND backstop_openings>0
            """,
            params,
        ).fetchone()
    return {
        "role_families": int(row["role_families"]),
        "active_listings": int(row["active_listings"]),
        "companies": int(row["companies"]),
        "new_24h": int(row["new_24h"]),
        "new_families_24h": int(row["new_families_24h"]),
        "verified_listings": int(row["verified_listings"]),
        "verified_families": int(row["verified_families"]),
        "sources": _catalog_count(),
        "leads": int(lead_row["leads"]),
        "lead_apps": int(lead_row["lead_apps"]),
    }


def _list_families(
    *,
    query: str,
    category: str,
    target: str,
    track: str,
    trust: str,
    location: str,
    sort: str,
    page: int,
    page_size: int,
    company: str = "",
    remote: bool = False,
    posted_within: int = 0,
) -> dict[str, object]:
    conditions: list[str] = []
    params: list[object] = []
    conditions.append(_target_clause(target, params))
    conditions.append(_tech_clause(category, track, params))
    conditions.append(_trust_clause(trust))
    if query:
        conditions.append(_search_clause(query, params))
    if location:
        conditions.append(_location_clause(location, params))
    if company:
        conditions.append("lower(company) = lower(%s)")
        params.append(company)
    if remote:
        conditions.append(
            "EXISTS (SELECT 1 FROM unnest(locations) AS value WHERE value ILIKE '%remote%')"
        )
    if posted_within:
        conditions.append(
            "COALESCE(latest_posted_at, first_detected_at) "
            ">= now() - (%s * interval '1 day')"
        )
        params.append(posted_within)
    order_params = list(params)
    order = _search_order(query, sort, order_params)
    where = " WHERE " + " AND ".join(f"({condition})" for condition in conditions)
    offset = max(0, page - 1) * page_size
    with db.connect() as connection:
        total = connection.execute(
            f"SELECT COUNT(*) AS count FROM families{where}", params
        ).fetchone()["count"]
        rows = connection.execute(
            f"""
            SELECT * FROM families{where}
            ORDER BY {order}
            LIMIT %s OFFSET %s
            """,
            [*order_params, page_size, offset],
        ).fetchall()
    return {"total": int(total), "items": [_present_family(row, trust=trust) for row in rows]}


@app.get("/api/families")
def families(
    q: str = Query("", max_length=200),
    category: str = "",
    target: str = "default",
    track: str = "tech",
    trust: str = "all",
    location: str = Query("", max_length=100),
    sort: str = "newest",
    page: int = Query(1, ge=1),
    page_size: int = Query(48, ge=12, le=100),
    company: str = Query("", max_length=100),
    remote: bool = False,
    posted_within: int = Query(0, ge=0, le=365),
) -> dict[str, object]:
    trust = trust.strip() or "all"
    if trust not in {"verified", "leads", "all"}:
        raise HTTPException(status_code=400, detail="trust must be verified, leads, or all")
    if sort not in {"newest", "verified", "company"}:
        raise HTTPException(status_code=400, detail="sort must be newest, verified, or company")
    return _list_families(
        query=q.strip(),
        category=category.strip(),
        target=target.strip(),
        track=track.strip(),
        trust=trust,
        location=location.strip(),
        sort=sort,
        page=page,
        page_size=page_size,
        company=company.strip(),
        remote=remote,
        posted_within=posted_within,
    )


@app.get("/api/facets")
def facets(trust: str = "verified", target: str = "default") -> dict[str, object]:
    if trust not in {"verified", "leads", "all"}:
        raise HTTPException(status_code=400, detail="trust must be verified, leads, or all")
    params: list[object] = []
    conditions = [_target_clause(target, params), _tech_clause("", "tech", params)]
    conditions.append(_trust_clause(trust))
    where = " WHERE " + " AND ".join(f"({condition})" for condition in conditions)
    with db.connect() as connection:
        companies = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT company AS value, COUNT(*) AS count
                FROM families{where}
                GROUP BY company
                ORDER BY count DESC, lower(company)
                """,
                params,
            ).fetchall()
        ]
        categories = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT category AS value, COUNT(*) AS count
                FROM families{where}
                GROUP BY category
                ORDER BY count DESC, category
                """,
                params,
            ).fetchall()
        ]
        remote_count = connection.execute(
            f"""
            SELECT COUNT(*) AS count FROM families{where}
            AND EXISTS (
                SELECT 1 FROM unnest(locations) AS value
                WHERE value ILIKE '%remote%'
            )
            """,
            params,
        ).fetchone()["count"]
    return {
        "companies": companies,
        "categories": categories,
        "remote_count": int(remote_count),
    }


@app.get("/api/families/{family_key}")
def family(family_key: str, trust: str = Query("verified")) -> dict[str, object]:
    if trust not in {"verified", "leads", "all"}:
        raise HTTPException(status_code=400, detail="trust must be verified, leads, or all")
    result = db.get_family(family_key)
    if result is None:
        raise HTTPException(status_code=404, detail="role family not found")
    return _present_family(result, trust=trust)


def _normalized_coverage() -> dict[str, object]:
    data = db.coverage()
    sources = list(data.get("sources") or [])
    current = [row for row in sources if str(row.get("scope") or "current") == "current"]
    actionable = [
        row
        for row in current
        if str(row.get("mode")) in {"board", "board-search", "domain"}
        and (row.get("last_error") or str(row.get("status")) in {"broken", "truncated"})
    ]
    contract = dict(data.get("contract") or {})
    contract["actionable_anomalies"] = len(actionable)
    contract["complete_enumerators"] = sum(
        bool(row.get("complete"))
        and str(row.get("status")) == "ok"
        and str(row.get("mode")) in {"board", "board-search", "domain"}
        for row in current
    )
    contract["query_scoped_boards"] = sum(
        str(row.get("mode")) == "board-search" for row in current
    )
    data["contract"] = contract
    return data


@app.get("/api/coverage")
def coverage() -> dict[str, object]:
    return _normalized_coverage()


@app.post("/api/sync", status_code=202)
async def sync() -> dict[str, object]:
    if os.getenv("GAIA_READ_ONLY", "0") == "1":
        raise HTTPException(
            status_code=503,
            detail="This deployment is read-only; run the crawler from a trusted worker.",
        )
    started = service.start_background("refresh")
    return {"started": started, **service.status()}


@app.post("/api/discover", status_code=202)
async def discover() -> dict[str, object]:
    if os.getenv("GAIA_READ_ONLY", "0") == "1":
        raise HTTPException(
            status_code=503,
            detail="This deployment is read-only; run discovery from a trusted worker.",
        )
    started = service.start_background("discover")
    return {"started": started, **service.status()}
