from __future__ import annotations

from collections.abc import Awaitable, Callable

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

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
    """Convert database failures on public API routes into truthful HTTP 503s."""

    @app.middleware("http")
    async def database_outage_guard(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        try:
            return await call_next(request)
        except Exception as error:
            if request.url.path.startswith("/api/") and is_database_outage(error):
                endpoint = request.url.path.removeprefix("/api/") or "api"
                return database_unavailable_response(error, endpoint)
            raise
