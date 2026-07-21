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


@app.get("/api/coverage")
def coverage() -> dict[str, object]:
    return db.coverage()


@app.post("/api/sync", status_code=202)
async def sync() -> dict[str, object]:
    started = service.start_background("refresh")
    return {"started": started, **service.status()}


@app.post("/api/discover", status_code=202)
async def discover() -> dict[str, object]:
    started = service.start_background("discover")
    return {"started": started, **service.status()}
