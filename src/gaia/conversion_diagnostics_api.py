from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Request

from .conversion_funnel import build_report, drain_candidates, failure_counts, reason_bucket
from .maintenance_api import _request_allowed


def install_conversion_diagnostics_api(app: FastAPI) -> None:
    """Install authenticated conversion reporting and bounded candidate draining."""
    if getattr(app.state, "gaia_conversion_diagnostics_installed", False):
        return
    app.state.gaia_conversion_diagnostics_installed = True

    def authorize(request: Request) -> None:
        if os.getenv("GAIA_ENABLE_CONVERSION_DIAGNOSTICS", "1") != "1":
            raise HTTPException(status_code=404, detail="conversion diagnostics disabled")
        if not _request_allowed(request):
            raise HTTPException(status_code=403, detail="maintenance caller not allowed")

    @app.get("/api/maintenance/diagnostics/conversion", include_in_schema=False)
    def conversion_diagnostics(
        request: Request,
        limit: int = 50,
        hours: int = 24,
    ) -> dict[str, object]:
        authorize(request)
        from . import api as legacy

        return build_report(legacy.db, hours=hours, limit=limit)

    @app.post(
        "/api/maintenance/diagnostics/drain-candidates",
        include_in_schema=False,
    )
    async def conversion_diagnostics_drain(
        request: Request,
        limit: int = 24,
        concurrency: int = 8,
        hours: int = 24,
    ) -> dict[str, object]:
        authorize(request)
        return await drain_candidates(
            limit=max(1, min(int(limit), 64)),
            concurrency=max(1, min(int(concurrency), 12)),
            hours=max(1, min(int(hours), 720)),
        )


__all__ = [
    "build_report",
    "drain_candidates",
    "failure_counts",
    "install_conversion_diagnostics_api",
    "reason_bucket",
]
