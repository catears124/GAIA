from __future__ import annotations

from collections.abc import Awaitable, Callable

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .snapshot_fallback import snapshot_response

_DATABASE_NOT_CONFIGURED = "PostgreSQL is not configured."


def is_database_outage(error: Exception) -> bool:
    """Recognize expected infrastructure failures without hiding application bugs."""
    return isinstance(error, (psycopg.Error, OSError, TimeoutError)) or (
        isinstance(error, RuntimeError) and str(error).startswith(_DATABASE_NOT_CONFIGURED)
    )


def database_unavailable_response(error: Exception, endpoint: str) -> JSONResponse:
    """Return stable, non-cacheable outage evidence to API clients."""
    return JSONResponse(
        status_code=503,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Retry-After": "30",
        },
        content={
            "ok": False,
            "stale": True,
            "reason": "database_unavailable",
            "endpoint": endpoint,
            "detail": type(error).__name__,
        },
    )


def install_database_outage_guard(app: FastAPI) -> None:
    """Install truthful inventory routes, then shield expected database outages."""
    from .inventory_truth_api import install_inventory_truth_api

    install_inventory_truth_api(app)

    @app.middleware("http")
    async def database_outage_guard(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        try:
            response = await call_next(request)
            if response.status_code == 503 and request.url.path in {"/api/families", "/api/stats"}:
                fallback = snapshot_response(request)
                if fallback is not None:
                    return fallback
            return response
        except Exception as error:
            if request.url.path.startswith("/api/") and is_database_outage(error):
                fallback = snapshot_response(request)
                if fallback is not None:
                    return fallback
                endpoint = request.url.path.removeprefix("/api/") or "api"
                return database_unavailable_response(error, endpoint)
            raise
