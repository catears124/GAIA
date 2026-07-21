from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .db import Database
from .quality import canonical_company, normalize_locations
from .service import SyncService

FRONTEND = Path(__file__).with_name("frontend")
TARGET_MATCHES = ("exact", "year_confirmed", "source_confirmed")
TECH_CATEGORIES = ("software", "ml-ai", "data", "security", "hardware", "quant", "product")
db = Database()
service = SyncService(db, concurrency=int(os.getenv("GAIA_CONCURRENCY", "16")))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if os.getenv("GAIA_INITIAL_SYNC", "1") == "1":
        service.start_background("refresh")
    yield
    await service.stop()


app = FastAPI(title="GAIA", version="3.0.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/health")
def health() -> dict[str, object]:
    return {"ok": True, **service.status()}


def _catalog_count() -> int:
    with db.connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_catalog'"
        ).fetchone()
        if exists:
            return int(connection.execute("SELECT COUNT(*) FROM source_catalog").fetchone()[0])
        return int(connection.execute("SELECT COUNT(*) FROM source_health").fetchone()[0])


def _target_clause(target: str, params: list[object]) -> str:
    if target == "default":
        placeholders = ",".join("?" for _ in TARGET_MATCHES)
        params.extend(TARGET_MATCHES)
        return f"target_match IN ({placeholders})"
    if target:
        params.append(target)
        return "target_match=?"
    return "1=1"


def _tech_clause(category: str, track: str, params: list[object]) -> str:
    if category:
        params.append(category)
        return "category=?"
    if track != "all":
        placeholders = ",".join("?" for _ in TECH_CATEGORIES)
        params.extend(TECH_CATEGORIES)
        return f"category IN ({placeholders})"
    return "1=1"


def _trust_clause(trust: str) -> str:
    if trust == "all":
        return "1=1"
    if trust == "leads":
        return "direct_openings=0 AND backstop_openings>0"
    # Product default: something employer-controlled recovered the application.
    return "direct_openings>0"


def _present_family(row: object) -> dict[str, object]:
    item = db._family_dict(row)  # noqa: SLF001 - API presentation layer intentionally reuses DB serializer.
    item["company"] = canonical_company(str(item.get("company") or ""))
    item["locations"] = normalize_locations(item.get("locations") or [])
    cleaned_openings: list[dict[str, object]] = []
    for opening in item.get("openings") or []:
        if isinstance(opening, dict):
            copy = dict(opening)
            copy["location"] = normalize_locations(copy.get("location") or [])
            cleaned_openings.append(copy)
    item["openings"] = cleaned_openings
    item["verified"] = int(item.get("direct_openings") or 0) > 0
    item["quality"] = "verified" if item["verified"] else "lead"
    return item


@app.get("/api/stats")
def stats() -> dict[str, int]:
    target_params: list[object] = []
    target_clause = _target_clause("default", target_params)
    tech_params: list[object] = []
    tech_clause = _tech_clause("", "tech", tech_params)
    with db.connect() as connection:
        row = connection.execute(
            f"""
            SELECT
+                COUNT(*) AS role_families,
+                COALESCE(SUM(opening_count), 0) AS active_listings,
+                COUNT(DISTINCT company) AS companies,
+                COALESCE(SUM(julianday(first_detected_at) >= julianday('now', '-1 day')), 0)
+                    AS new_24h
+            FROM families
+            WHERE {target_clause}
+              AND {tech_clause}
+              AND direct_openings>0
+            """.replace("+", ""),
            [*target_params, *tech_params],
        ).fetchone()
        lead_row = connection.execute(
            f"""
            SELECT COUNT(*) AS leads, COALESCE(SUM(opening_count),0) AS lead_apps
            FROM families
            WHERE {target_clause}
              AND {tech_clause}
              AND direct_openings=0
              AND backstop_openings>0
            """,
            [*target_params, *tech_params],
        ).fetchone()
    return {
        "role_families": int(row["role_families"]),
        "active_listings": int(row["active_listings"]),
        "companies": int(row["companies"]),
        "new_24h": int(row["new_24h"]),
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
    page: int,
    page_size: int,
) -> dict[str, object]:
    conditions: list[str] = []
    params: list[object] = []
    conditions.append(_target_clause(target, params))
    conditions.append(_tech_clause(category, track, params))
    conditions.append(_trust_clause(trust))
    if query:
        conditions.append("(company LIKE ? OR title LIKE ? OR locations_json LIKE ?)")
        needle = f"%{query}%"
        params.extend([needle, needle, needle])
    where = " WHERE " + " AND ".join(f"({condition})" for condition in conditions)
    offset = max(0, page - 1) * page_size
    with db.connect() as connection:
        total = int(
            connection.execute(f"SELECT COUNT(*) FROM families{where}", params).fetchone()[0]
        )
        rows = connection.execute(
            f"""
            SELECT * FROM families{where}
            ORDER BY
                CASE WHEN latest_posted_at IS NULL THEN 1 ELSE 0 END,
                latest_posted_at DESC,
                last_verified_at DESC,
                first_detected_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        ).fetchall()
    return {"total": total, "items": [_present_family(row) for row in rows]}


@app.get("/api/families")
def families(
    q: str = "",
    category: str = "",
    target: str = "default",
    track: str = "tech",
    trust: str = "verified",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=20, le=250),
) -> dict[str, object]:
    trust = trust.strip() or "verified"
    if trust not in {"verified", "leads", "all"}:
        raise HTTPException(status_code=400, detail="trust must be verified, leads, or all")
    return _list_families(
        query=q.strip(),
        category=category.strip(),
        target=target.strip(),
        track=track.strip(),
        trust=trust,
        page=page,
        page_size=page_size,
    )


@app.get("/api/families/{family_key}")
def family(family_key: str) -> dict[str, object]:
    result = db.get_family(family_key)
    if result is None:
        raise HTTPException(status_code=404, detail="role family not found")
    # Round-trip through JSON-like shape so the drawer gets the same v3 cleanup as the table.
    class Row(dict):
        pass

    row = Row(result)
    row["locations_json"] = json.dumps(result.get("locations") or [])
    row["openings_json"] = json.dumps(result.get("openings") or [])
    return _present_family(row)


def _normalized_coverage() -> dict[str, object]:
    data = db.coverage()
    sources = list(data.get("sources") or [])
    current = [row for row in sources if str(row.get("scope") or "current") == "current"]
    actionable = [
        row
        for row in current
        if row.get("last_error")
        or str(row.get("status")) in {"broken", "truncated"}
        or (
            str(row.get("status")) == "empty"
            and str(row.get("mode")) in {"board", "domain"}
        )
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
    started = service.start_background("refresh")
    return {"started": started, **service.status()}


@app.post("/api/discover", status_code=202)
async def discover() -> dict[str, object]:
    started = service.start_background("discover")
    return {"started": started, **service.status()}
