from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from gaia.coverage_api import SOURCE_DIAGNOSTICS_SQL, install_coverage_api


def test_source_diagnostics_start_from_configured_targets() -> None:
    normalized = " ".join(SOURCE_DIAGNOSTICS_SQL.split()).lower()

    assert "from crawl_targets as target" in normalized
    assert "full outer join source_health as health using(source)" in normalized
    assert "coalesce(target.source, health.source) as source" in normalized
    assert "target.last_complete_at" in normalized
    assert "catalog.kind as catalog_kind" in normalized


def test_install_replaces_the_legacy_get_coverage_route() -> None:
    app = FastAPI()

    @app.get("/api/coverage")
    def legacy_route():
        return {"legacy": True}

    install_coverage_api(app)
    matching = [
        route
        for route in app.router.routes
        if isinstance(route, APIRoute)
        and route.path == "/api/coverage"
        and "GET" in route.methods
    ]

    assert len(matching) == 1
    assert matching[0].endpoint.__name__ == "live_coverage"


def test_install_is_idempotent() -> None:
    app = FastAPI()

    install_coverage_api(app)
    install_coverage_api(app)

    matching = [
        route
        for route in app.router.routes
        if isinstance(route, APIRoute) and route.path == "/api/coverage"
    ]
    assert len(matching) == 1
