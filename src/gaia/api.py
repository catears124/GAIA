from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .db import Database
from .service import SyncService

FRONTEND = Path(__file__).with_name("frontend")
TARGETS = "('exact','year_confirmed','source_confirmed')"
db = Database()
service = SyncService(db, concurrency=int(os.getenv("GAIA_CONCURRENCY", "16")))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if os.getenv("GAIA_INITIAL_SYNC", "1") == "1":
        service.start_background("refresh")
    yield
    await service.stop()


app = FastAPI(title="GAIA", version="2.0.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/health")
def health() -> dict[str, object]:
    return {"ok": True, **service.status()}


@app.get("/api/stats")
def stats() -> dict[str, int]:
    with db.connect() as connection:
        row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS role_families,
                COALESCE(SUM(opening_count), 0) AS active_listings,
                COUNT(DISTINCT company) AS companies,
                COALESCE(SUM(julianday(first_detected_at) >= julianday('now', '-1 day')), 0)
                    AS new_24h
            FROM families
            WHERE target_match IN {TARGETS}
              AND category != 'other'
            """
        ).fetchone()
        source_count = int(
            connection.execute("SELECT COUNT(*) FROM source_health").fetchone()[0]
        )
    return {
        "role_families": int(row["role_families"]),
        "active_listings": int(row["active_listings"]),
        "companies": int(row["companies"]),
        "new_24h": int(row["new_24h"]),
        "sources": source_count,
    }


@app.get("/api/families")
def families(
    q: str = "",
    category: str = "",
    target: str = "default",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=20, le=250),
) -> dict[str, object]:
    return db.list_families(
        query=q.strip(),
        category=category.strip(),
        target=target.strip(),
        page=page,
        page_size=page_size,
    )


@app.get("/api/families/{family_key}")
def family(family_key: str) -> dict[str, object]:
    result = db.get_family(family_key)
    if result is None:
        raise HTTPException(status_code=404, detail="role family not found")
    return result


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
