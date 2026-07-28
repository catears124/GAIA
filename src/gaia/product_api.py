from __future__ import annotations

from fastapi.routing import APIRoute

from . import api as legacy

app = legacy.app


def _live_order_clause(sort: str) -> str:
    if sort == "company":
        return "lower(company), lower(title), family_key"
    if sort == "verified":
        return "last_verified_at DESC, first_detected_at DESC, family_key"
    # "Newest" means newly discovered by GAIA. Employer-published dates remain
    # visible metadata, but they must not bury a role that entered the inventory now.
    return (
        "first_detected_at DESC, "
        "COALESCE(latest_posted_at, first_detected_at) DESC, "
        "last_verified_at DESC, family_key"
    )


legacy._order_clause = _live_order_clause

# Replace only the old stats endpoint; all existing query and presentation helpers remain shared.
app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (
        isinstance(route, APIRoute)
        and route.path == "/api/stats"
        and "GET" in route.methods
    )
]


@app.get("/api/stats")
def live_stats() -> dict[str, int]:
    with legacy.db.connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS role_families,
                COALESCE(SUM(direct_openings), 0) AS active_listings,
                COUNT(DISTINCT company) AS companies,
                COUNT(*) FILTER (
                    WHERE first_detected_at >= date_trunc('day', now())
                ) AS new_families_today,
                COALESCE(SUM(direct_openings), 0) AS verified_listings,
                COUNT(*) AS verified_families
            FROM families
            WHERE target_match!='not_internship'
              AND category = ANY(%s)
              AND direct_openings>0
            """,
            (list(legacy.TECH_CATEGORIES),),
        ).fetchone()
        lead_row = connection.execute(
            """
            SELECT COUNT(*) AS leads, COALESCE(SUM(backstop_openings),0) AS lead_apps
            FROM families
            WHERE target_match!='not_internship'
              AND category = ANY(%s)
              AND direct_openings=0
              AND backstop_openings>0
            """,
            (list(legacy.TECH_CATEGORIES),),
        ).fetchone()
        movement = connection.execute(
            """
            SELECT
                COUNT(DISTINCT canonical_apply_url) FILTER (
                    WHERE first_seen_at >= date_trunc('day', now())
                ) AS new_today,
                COUNT(DISTINCT canonical_apply_url) FILTER (
                    WHERE removed_at >= date_trunc('day', now())
                ) AS removed_today
            FROM postings
            WHERE source_mode='direct'
              AND target_match!='not_internship'
              AND category = ANY(%s)
            """,
            (list(legacy.TECH_CATEGORIES),),
        ).fetchone()
        source_row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM crawl_targets AS target
            JOIN source_catalog AS catalog USING(source)
            WHERE target.enabled AND target.scheduled AND catalog.validated
            """
        ).fetchone()

    new_today = int(movement["new_today"] or 0)
    removed_today = int(movement["removed_today"] or 0)
    return {
        "role_families": int(row["role_families"]),
        "active_listings": int(row["active_listings"]),
        "companies": int(row["companies"]),
        "new_24h": new_today,
        "new_today": new_today,
        "removed_today": removed_today,
        "net_today": new_today - removed_today,
        "new_families_24h": int(row["new_families_today"]),
        "verified_listings": int(row["verified_listings"]),
        "verified_families": int(row["verified_families"]),
        "sources": int(source_row["count"] or 0),
        "leads": int(lead_row["leads"]),
        "lead_apps": int(lead_row["lead_apps"]),
    }
