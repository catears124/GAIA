from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

LOGGER = logging.getLogger("gaia.request-bootstrap")
_LOCK = asyncio.Lock()
_READY = False
_NEXT_ATTEMPT_AT = 0.0


async def _ensure_runtime_database() -> None:
    global _NEXT_ATTEMPT_AT, _READY
    if _READY or os.getenv("GAIA_BOOTSTRAP_EMPTY_DATABASE", "1") != "1":
        return
    now = time.monotonic()
    if now < _NEXT_ATTEMPT_AT:
        return
    async with _LOCK:
        if _READY:
            return
        now = time.monotonic()
        if now < _NEXT_ATTEMPT_AT:
            return
        try:
            # This import intentionally occurs only inside a real request. Vercel's
            # build-time `from app import app` check never imports crawler modules or
            # opens a database connection.
            from .runtime_bootstrap import bootstrap_empty_database

            _READY = bool(await bootstrap_empty_database())
        except Exception:
            LOGGER.exception("request-time database bootstrap crashed")
            _READY = False
        if not _READY:
            _NEXT_ATTEMPT_AT = time.monotonic() + 30.0


def install_request_bootstrap(app: FastAPI) -> None:
    if getattr(app.state, "gaia_request_bootstrap_installed", False):
        return
    app.state.gaia_request_bootstrap_installed = True

    @app.middleware("http")
    async def request_bootstrap(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path.startswith("/api/"):
            await _ensure_runtime_database()
        return await call_next(request)
