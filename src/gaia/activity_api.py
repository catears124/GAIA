from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute


def _remove_stats_route(app: FastAPI) -> None:
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            isinstance(route, APIRoute)
            and route.path == "/api/stats"
            and "GET" in route.methods
        )
    ]


def install_activity_api(app: FastAPI) -> None:
    """Expose posting freshness and GAIA discovery as separate durable events."""
    if getattr(app.state, "gaia_activity_api_installed", False):
        return
    app.state.gaia_activity_api_installed = True
    _remove_stats_route(app)

    @app.get("/api/stats")
    def live_stats() -> dict[str, object]:
        from . import api as legacy
        from .product_api import _activity_stats
        from .universe import universe_summary

        tech_categories = list(legacy.TECH_CATEGORIES)
        current_cycle = "(year IS NULL OR year >= EXTRACT(YEAR FROM now())::int)"
        with legacy.db.connect() as connection:
            active = connection.execute(
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
                WHERE {current_cycle}
                  AND category = ANY(%s)
                  AND direct_openings>0
                """,
                (tech_categories,),
            ).fetchone()
            discovery = connection.execute(
                f"""
                SELECT COUNT(DISTINCT family_key) AS discovered_families_today
                FROM postings
                WHERE first_seen_at >= now() - interval '24 hours'
                  AND source_mode='direct'
                  AND {current_cycle}
                  AND category = ANY(%s)
                """,
                (tech_categories,),
            ).fetchone()
            lead_row = connection.execute(
                f"""
                SELECT COUNT(*) AS leads, COALESCE(SUM(backstop_openings),0) AS lead_apps
                FROM families
                WHERE {current_cycle}
                  AND category = ANY(%s)
                  AND direct_openings=0
                  AND backstop_openings>0
                """,
                (tech_categories,),
            ).fetchone()
            movement = connection.execute(
                f"""
                SELECT
                    COUNT(DISTINCT canonical_apply_url) FILTER (
                        WHERE first_seen_at >= now() - interval '24 hours'
                    ) AS new_urls_today,
                    COUNT(DISTINCT canonical_apply_url) FILTER (
                        WHERE NOT active
                          AND removed_at >= now() - interval '24 hours'
                    ) AS removed_urls_today
                FROM postings
                WHERE source_mode='direct'
                  AND {current_cycle}
                  AND category = ANY(%s)
                """,
                (tech_categories,),
            ).fetchone()
            source_row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM crawl_targets AS target
                JOIN source_catalog AS catalog USING(source)
                WHERE target.enabled
                  AND target.scheduled
                  AND catalog.validated
                  AND catalog.scope='current'
                """
            ).fetchone()

        census = universe_summary(legacy.db, limit=1)
        census_summary = dict(census.get("summary") or {})
        activity_row = {
            "new_families_today": int(active["new_families_today"] or 0),
            "discovered_families_today": int(discovery["discovered_families_today"] or 0),
        }
        return {
            "role_families": int(active["role_families"]),
            "active_listings": int(active["active_listings"]),
            "companies": int(active["companies"]),
            **_activity_stats(activity_row, movement),
            "verified_listings": int(active["verified_listings"]),
            "verified_families": int(active["verified_families"]),
            "validated_sources": int(source_row["count"] or 0),
            "known_employers": int(census_summary.get("known_employers") or 0),
            "enumerated_employers": int(census_summary.get("enumerated_employers") or 0),
            "unresolved_employers": int(census_summary.get("unresolved_employers") or 0),
            "blind_spots": int(census_summary.get("blind_spots") or 0),
            "leads": int(lead_row["leads"]),
            "lead_apps": int(lead_row["lead_apps"]),
        }
