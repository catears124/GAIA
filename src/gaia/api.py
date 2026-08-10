from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .db import Database
from .quality import (
    TECH_CATEGORIES as BASE_TECH_CATEGORIES,
)
from .quality import (
    canonical_company,
    is_index_mode,
    normalize_locations,
)

FRONTEND = Path(__file__).with_name("frontend")
TARGET_MATCHES = ("exact", "year_confirmed", "source_confirmed")
TECH_CATEGORIES = (*BASE_TECH_CATEGORIES, "other-technical")
db = Database()
app = FastAPI(title="GAIA", version="5.0.0")
app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")


@app.get("/", include_in_schema=False)
def root(request: Request) -> HTMLResponse:
    content = (FRONTEND / "index.html").read_text(encoding="utf-8")
    og_image = request.url_for("assets", path="og.png")
    return HTMLResponse(content.replace("__GAIA_OG_IMAGE__", str(og_image)))


def _inventory_state() -> dict[str, object]:
    with db.connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE target.enabled AND catalog.scope='current') AS total,
                COUNT(*) FILTER (
                    WHERE target.enabled AND catalog.scope='current'
                      AND target.lease_expires_at>now()
                ) AS running,
                COUNT(*) FILTER (
                    WHERE target.enabled AND catalog.scope='current'
                      AND target.last_complete_at IS NULL
                ) AS never_completed,
                COUNT(*) FILTER (
                    WHERE target.enabled AND catalog.scope='current'
                      AND target.last_complete_at IS NOT NULL
                      AND target.last_complete_at <
                          now() - make_interval(secs => target.interval_seconds * 2)
                ) AS overdue,
                COUNT(*) FILTER (
                    WHERE target.enabled AND catalog.scope='current'
                      AND target.last_status IN ('broken','blocked','truncated','partial')
                ) AS degraded,
                MAX(target.last_finished_at) FILTER (
                    WHERE target.enabled AND catalog.scope='current'
                ) AS latest_activity_at,
                MIN(target.last_complete_at) FILTER (
                    WHERE target.enabled AND catalog.scope='current'
                ) AS coverage_watermark,
                COUNT(*) FILTER (WHERE target.enabled AND catalog.scope='historical') AS historical
            FROM crawl_targets AS target
            JOIN source_catalog AS catalog USING(source)
            """
        ).fetchone()
    state = db._json_row(row)  # noqa: SLF001
    for key in ("total", "running", "never_completed", "overdue", "degraded", "historical"):
        state[key] = int(state.get(key) or 0)
    total = int(state["total"])
    incomplete = min(
        total,
        int(state["never_completed"]) + int(state["overdue"]) + int(state["degraded"]),
    )
    state["fresh"] = max(0, total - incomplete)
    state["fresh_percent"] = round(100 * int(state["fresh"]) / total, 1) if total else 0.0
    state["healthy"] = bool(total) and incomplete == 0
    return state


@app.get("/api/health")
def health() -> dict[str, object]:
    inventory = _inventory_state()
    fully_initialized = int(inventory["never_completed"]) == 0 and int(inventory["total"]) > 0
    watermark = inventory.get("coverage_watermark") if fully_initialized else None
    failing = int(inventory["never_completed"]) + int(inventory["overdue"]) + int(
        inventory["degraded"]
    )
    return {
        "ok": bool(inventory["healthy"]),
        "read_only": os.getenv("GAIA_READ_ONLY", "0") == "1",
        "running": int(inventory["running"]) > 0,
        "progress": {
            "mode": "continuous-inventory",
            "stage": "crawling" if int(inventory["running"]) else "scheduled",
            "completed": int(inventory["fresh"]),
            "total": int(inventory["total"]),
            "current": None,
            "started_at": None,
            "elapsed_seconds": 0,
        },
        "last_summary": None,
        "data": {
            "last_run": (
                {"finished_at": watermark, "status": "ok" if inventory["healthy"] else "degraded"}
                if watermark
                else None
            ),
            "last_success_at": watermark,
            "sources": int(inventory["total"]),
            "failing_sources": failing,
        },
        "inventory": inventory,
    }


def _catalog_count() -> int:
    with db.connect() as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM source_catalog").fetchone()
    return int(row["count"])


def _target_clause(target: str, params: list[object]) -> str:
    """Apply public cycle semantics to explicit year/season metadata."""
    if target == "exact":
        params.extend([2027, "summer"])
        return "year=%s AND lower(COALESCE(season,''))=%s"
    if target in {"default", "year_confirmed"}:
        params.append(2027)
        return "year=%s"
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
    item = db._family_dict(row)  # noqa: SLF001
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
                COUNT(*) FILTER (
                    WHERE latest_posted_at >= now() - interval '24 hours'
                ) AS new_families_today,
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
        movement = connection.execute(
            """
            SELECT
                COUNT(DISTINCT canonical_apply_url) FILTER (
                    WHERE first_seen_at >= now() - interval '24 hours'
                ) AS new_today,
                COUNT(DISTINCT canonical_apply_url) FILTER (
                    WHERE removed_at >= now() - interval '24 hours'
                ) AS removed_today
            FROM postings
            WHERE source_mode='direct'
              AND year=2027
              AND category = ANY(%s)
            """,
            (list(TECH_CATEGORIES),),
        ).fetchone()
    new_verified = int(row["new_families_today"] or 0)
    discovered_urls = int(movement["new_today"] or 0)
    removed_today = int(movement["removed_today"] or 0)
    return {
        "role_families": int(row["role_families"]),
        "active_listings": int(row["active_listings"]),
        "companies": int(row["companies"]),
        "new_24h": new_verified,
        "new_today": new_verified,
        "removed_today": removed_today,
        "net_today": discovered_urls - removed_today,
        "new_families_24h": new_verified,
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
        # Never substitute GAIA's own first-detected time for a posting date.
        conditions.append("latest_posted_at >= now() - (%s * interval '1 day')")
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
    return {"companies": companies, "categories": categories, "remote_count": int(remote_count)}


@app.get("/api/families/{family_key}")
def family(family_key: str, trust: str = Query("verified")) -> dict[str, object]:
    if trust not in {"verified", "leads", "all"}:
        raise HTTPException(status_code=400, detail="trust must be verified, leads, or all")
    result = db.get_family(family_key)
    if result is None:
        raise HTTPException(status_code=404, detail="role family not found")
    return _present_family(result, trust=trust)


@app.get("/api/coverage")
def coverage() -> dict[str, object]:
    data = db.coverage()
    with db.connect() as connection:
        rows = connection.execute(
            """
            SELECT health.*, target.enabled, target.priority, target.interval_seconds,
                   target.next_run_at, target.lease_expires_at, target.last_complete_at,
                   target.last_status AS crawl_status, target.last_rows AS crawl_rows,
                   target.consecutive_failures AS crawl_failures
            FROM source_health AS health
            LEFT JOIN crawl_targets AS target USING(source)
            ORDER BY health.source
            """
        ).fetchall()
    sources = [db._json_row(row) for row in rows]  # noqa: SLF001
    inventory = _inventory_state()
    contract = dict(data.get("contract") or {})
    contract.update(
        {
            "continuous_inventory": True,
            "configured_sources": int(inventory["total"]),
            "fresh_sources": int(inventory["fresh"]),
            "fresh_percent": inventory["fresh_percent"],
            "never_completed": int(inventory["never_completed"]),
            "overdue_sources": int(inventory["overdue"]),
            "degraded_sources": int(inventory["degraded"]),
            "historical_sources": int(inventory["historical"]),
            "coverage_watermark": inventory.get("coverage_watermark"),
        }
    )
    data["contract"] = contract
    data["sources"] = sources
    return data
