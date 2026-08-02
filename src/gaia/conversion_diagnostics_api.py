from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, HTTPException, Request

from .conversion_funnel import (
    build_report,
    failure_counts,
    reason_bucket,
    repair_publication,
)
from .fast_candidate_drain import drain_candidates
from .lead_promotion import promote_leads
from .maintenance_api import _request_allowed


def install_conversion_diagnostics_api(app: FastAPI) -> None:
    """Install authenticated conversion reporting and bounded repair actions."""
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
        timeout_seconds: int = 42,
    ) -> dict[str, object]:
        authorize(request)
        bounded_limit = max(1, min(int(limit), 64))
        bounded_concurrency = max(1, min(int(concurrency), 12))
        bounded_timeout = max(5, min(int(timeout_seconds), 45))
        try:
            return await asyncio.wait_for(
                drain_candidates(
                    limit=bounded_limit,
                    concurrency=bounded_concurrency,
                    hours=max(1, min(int(hours), 720)),
                ),
                timeout=bounded_timeout,
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=408,
                detail={
                    "status": "candidate_drain_timeout",
                    "limit": bounded_limit,
                    "concurrency": bounded_concurrency,
                    "timeout_seconds": bounded_timeout,
                },
            ) from exc

    @app.post(
        "/api/maintenance/diagnostics/promote-leads",
        include_in_schema=False,
    )
    async def conversion_diagnostics_promote_leads(
        request: Request,
        limit: int = 12,
        concurrency: int = 6,
        hours: int = 24,
        max_age_days: int = 14,
        timeout_seconds: int = 42,
    ) -> dict[str, object]:
        authorize(request)
        bounded_limit = max(1, min(int(limit), 64))
        bounded_concurrency = max(1, min(int(concurrency), 12))
        bounded_timeout = max(5, min(int(timeout_seconds), 45))
        try:
            return await asyncio.wait_for(
                promote_leads(
                    limit=bounded_limit,
                    concurrency=bounded_concurrency,
                    hours=max(1, min(int(hours), 720)),
                    max_age_days=max(1, min(int(max_age_days), 90)),
                ),
                timeout=bounded_timeout,
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=408,
                detail={
                    "status": "lead_promotion_timeout",
                    "limit": bounded_limit,
                    "concurrency": bounded_concurrency,
                    "timeout_seconds": bounded_timeout,
                },
            ) from exc

    @app.post(
        "/api/maintenance/diagnostics/repair-publication",
        include_in_schema=False,
    )
    def conversion_diagnostics_repair_publication(
        request: Request,
        limit: int = 50,
        hours: int = 24,
    ) -> dict[str, object]:
        authorize(request)
        from . import api as legacy

        return repair_publication(
            legacy.db,
            hours=max(1, min(int(hours), 720)),
            limit=max(1, min(int(limit), 200)),
        )


__all__ = [
    "build_report",
    "drain_candidates",
    "failure_counts",
    "install_conversion_diagnostics_api",
    "promote_leads",
    "reason_bucket",
    "repair_publication",
]
