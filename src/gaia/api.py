from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query

from . import db
from .company_normalization import canonical_company
from .location_normalization import normalize_locations
from .models import canonical_url
from .source_policy import is_index_mode

app = FastAPI(title="GAIA", version="5")
TARGET_MATCHES = {"exact", "year_confirmed", "source_confirmed"}
TECH_CATEGORIES = {
    "software",
    "ml-ai",
    "quant",
    "security",
    "data",
    "product",
    "hardware",
    "other-technical",
}


def _catalog_count() -> int:
    with db.connect() as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM source_catalog").fetchone()
    return int(row["count"])


def _target_clause(target: str, params: list[object]) -> str:
    """Map product cycle filters onto explicit year/season metadata.

    Internal classifier labels are evidence metadata, not product semantics. A role
    from a Summer-2027-scoped source can be `source_confirmed` and still belongs in
    the public Summer 2027 view. Conversely, a Spring 2027 role belongs in Any 2027
    even when it is `wrong_season` relative to a summer classifier target.
    """
    if target == "exact":
        params.extend([2027, "summer"])
        return "year=%s AND lower(COALESCE(season,''))=%s"
    if target in {"default", "year_confirmed"}:
        params.append(2027)
        return "year=%s"
    if target:
        params.append(target)
        return "target_match=%s"
    return "TRUE"


def _tech_clause(category: str, track: str, params: list[object]) -> str:
    if category:
        params.append(category)
        return "category=%s"
    if track != "all":
        params.append(list(TECH_CATEGORIES))
        return "category = ANY(%s)"
    return "TRUE"


def _trust_clause(trust: str) -> str:
    if trust == "all":
        return "TRUE"
    if trust == "leads":
        return "direct_openings=0 AND backstop_openings>0"
    return "direct_openings>0"


def _escape_like(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _search_clause(query: str, params: list[object]) -> str:
    clauses: list[str] = []
    for token in query.split():
        pattern = f"%{_escape_like(token)}%"
        clauses.append(
            "(company ILIKE %s ESCAPE '!' OR title ILIKE %s ESCAPE '!' "
            "OR array_to_string(locations, ' ') ILIKE %s ESCAPE '!')"
        )
        params.extend([pattern, pattern, pattern])
    return " AND ".join(clauses) or "TRUE"


def _location_clause(location: str, params: list[object]) -> str:
    if re.fullmatch(r"[A-Za-z]{2}", location):
        code = location.upper()
        params.extend([code, f"%, {code}"])
        return (
            "EXISTS (SELECT 1 FROM unnest(locations) AS value "
            "WHERE value = %s OR value ILIKE %s)"
        )
    params.append(f"%{_escape_like(location)}%")
    return (
        "EXISTS (SELECT 1 FROM unnest(locations) AS value "
        "WHERE value ILIKE %s ESCAPE '!')"
    )


def _search_order(query: str, sort: str, params: list[object]) -> str:
    if not query:
        return _order_clause(sort)
    phrase = _escape_like(query)
    first = query.split()[0]
    first_pattern = _escape_like(first)
    params.extend([query, first, f"{phrase}%", f"{first_pattern}%", f"%{phrase}%"])
    return (
        "CASE "
        "WHEN lower(company) = lower(%s) THEN 0 "
        "WHEN lower(company) = lower(%s) THEN 1 "
        "WHEN company ILIKE %s ESCAPE '!' THEN 2 "
        "WHEN company ILIKE %s ESCAPE '!' THEN 3 "
        "WHEN title ILIKE %s ESCAPE '!' THEN 4 "
        "ELSE 5 END, "
        f"{_order_clause(sort)}"
    )


def _order_clause(sort: str) -> str:
    if sort == "company":
        return "lower(company), lower(title), family_key"
    if sort == "verified":
        return "last_verified_at DESC, first_detected_at DESC, family_key"
    return (
        "COALESCE(latest_posted_at, first_detected_at) DESC, "
        "last_verified_at DESC, first_detected_at DESC, family_key"
    )


def _opening_is_lead(opening: dict[str, object]) -> bool:
    return is_index_mode(str(opening.get("source_mode") or ""))


def _opening_is_direct(opening: dict[str, object]) -> bool:
    return str(opening.get("source_mode") or "") == "direct"


def _present_family(row: Mapping[str, object], *, trust: str = "all") -> dict[str, object]:
    item = db._family_dict(row)  # noqa: SLF001
    item["company"] = canonical_company(str(item.get("company") or ""))
    item["locations"] = normalize_locations(item.get("locations") or [])
    cleaned_openings: list[dict[str, object]] = []
    for opening in item.get("openings") or []:
        if not isinstance(opening, dict):
            continue
        if trust == "verified" and not _opening_is_direct(opening):
            continue
        if trust == "leads" and not _opening_is_lead(opening):
            continue
        copy = dict(opening)
        copy["location"] = normalize_locations(copy.get("location") or [])
        cleaned_openings.append(copy)
    item["openings"] = cleaned_openings
    if trust in {"verified", "leads"}:
        item["opening_count"] = len(cleaned_openings)
    item["locations"] = normalize_locations(
        [location for opening in cleaned_openings for location in opening.get("location", [])]
        or item["locations"]
    )
    item["location_count"] = len(item["locations"])
    item["verified"] = int(item.get("direct_openings") or 0) > 0
    item["quality"] = "verified" if item["verified"] else "lead"
    return item


@app.get("/api/stats")
def stats() -> dict[str, int]:
    target_params: list[object] = []
    target_clause = _target_clause("default", target_params)
    tech_params: list[object] = []
    tech_clause = _tech_clause("", "tech", tech_params)
    params = [*target_params, *tech_params]
    with db.connect() as connection:
        row = connection.execute(
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
            WHERE {target_clause}
              AND {tech_clause}
              AND direct_openings>0
            """,
            params,
        ).fetchone()
        lead_row = connection.execute(
            f"""
            SELECT COUNT(*) AS leads, COALESCE(SUM(backstop_openings),0) AS lead_apps
            FROM families
            WHERE {target_clause}
              AND {tech_clause}
              AND direct_openings=0
              AND backstop_openings>0
            """,
            params,
        ).fetchone()
        movement = connection.execute(
            """
            SELECT
                COUNT(DISTINCT canonical_apply_url) FILTER (
                    WHERE first_seen_at >= now() - interval '24 hours'
                ) AS new_today,
                COUNT(DISTINCT canonical_apply_url) FILTER (
                    WHERE removed_at >= now() - interval '24 hours'
                ) AS removed_today
            FROM postings
            WHERE source_mode='direct'
              AND year=2027
              AND category = ANY(%s)
            """,
            (list(TECH_CATEGORIES),),
        ).fetchone()
    new_today = int(row["new_families_today"] or 0)
    removed_today = int(movement["removed_today"] or 0)
    return {
        "role_families": int(row["role_families"]),
        "active_listings": int(row["active_listings"]),
        "companies": int(row["companies"]),
        "new_24h": new_today,
        "new_today": new_today,
        "removed_today": removed_today,
        "net_today": int(movement["new_today"] or 0) - removed_today,
        "new_families_24h": new_today,
        "verified_listings": int(row["verified_listings"]),
        "verified_families": int(row["verified_families"]),
        "sources": _catalog_count(),
        "leads": int(lead_row["leads"]),
        "lead_apps": int(lead_row["lead_apps"]),
    }


def _list_families(
    *,
    query: str,
    category: str,
    target: str,
    track: str,
    trust: str,
    location: str,
    sort: str,
    page: int,
    page_size: int,
    company: str = "",
    remote: bool = False,
    posted_within: int = 0,
) -> dict[str, object]:
    conditions: list[str] = []
    params: list[object] = []
    conditions.append(_target_clause(target, params))
    conditions.append(_tech_clause(category, track, params))
    conditions.append(_trust_clause(trust))
    if query:
        conditions.append(_search_clause(query, params))
    if location:
        conditions.append(_location_clause(location, params))
    if company:
        conditions.append("lower(company) = lower(%s)")
        params.append(company)
    if remote:
        conditions.append(
            "EXISTS (SELECT 1 FROM unnest(locations) AS value WHERE value ILIKE '%remote%')"
        )
    if posted_within:
        # The live relational model currently persists employer/ATS dates only.
        # Do not substitute GAIA's first-detected time and call an old recovered
        # role "posted today". Sensor-reported dates are available in v4 snapshots.
        conditions.append("latest_posted_at >= now() - (%s * interval '1 day')")
        params.append(posted_within)
    order_params = list(params)
    order = _search_order(query, sort, order_params)
    where = " WHERE " + " AND ".join(f"({condition})" for condition in conditions)
    offset = max(0, page - 1) * page_size
    with db.connect() as connection:
        total = connection.execute(
            f"SELECT COUNT(*) AS count FROM families{where}", params
        ).fetchone()["count"]
        rows = connection.execute(
            f"""
            SELECT * FROM families{where}
            ORDER BY {order}
            LIMIT %s OFFSET %s
            """,
            [*order_params, page_size, offset],
        ).fetchall()
    return {"total": int(total), "items": [_present_family(row, trust=trust) for row in rows]}


@app.get("/api/families")
def families(
    q: str = Query("", max_length=200),
    category: str = "",
    target: str = "default",
    track: str = "tech",
    trust: str = "all",
    location: str = Query("", max_length=100),
    sort: str = "newest",
    page: int = Query(1, ge=1),
    page_size: int = Query(48, ge=12, le=100),
    company: str = Query("", max_length=100),
    remote: bool = False,
    posted_within: int = Query(0, ge=0, le=365),
) -> dict[str, object]:
    trust = trust.strip() or "all"
    if trust not in {"verified", "leads", "all"}:
        raise HTTPException(status_code=400, detail="trust must be verified, leads, or all")
    if sort not in {"newest", "verified", "company"}:
        raise HTTPException(status_code=400, detail="sort must be newest, verified, or company")
    return _list_families(
        query=q.strip(),
        category=category.strip(),
        target=target.strip(),
        track=track.strip(),
        trust=trust,
        location=location.strip(),
        sort=sort,
        page=page,
        page_size=page_size,
        company=company.strip(),
        remote=remote,
        posted_within=posted_within,
    )


@app.get("/api/facets")
def facets(trust: str = "verified", target: str = "default") -> dict[str, object]:
    if trust not in {"verified", "leads", "all"}:
        raise HTTPException(status_code=400, detail="trust must be verified, leads, or all")
    params: list[object] = []
    conditions = [_target_clause(target, params), _tech_clause("", "tech", params)]
    conditions.append(_trust_clause(trust))
    where = " WHERE " + " AND ".join(f"({condition})" for condition in conditions)
    with db.connect() as connection:
        companies = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT company AS value, COUNT(*) AS count
                FROM families{where}
                GROUP BY company
                ORDER BY count DESC, lower(company)
                """,
                params,
            ).fetchall()
        ]
        categories = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT category AS value, COUNT(*) AS count
                FROM families{where}
                GROUP BY category
                ORDER BY count DESC, category
                """,
                params,
            ).fetchall()
        ]
        remote_count = connection.execute(
            f"""
            SELECT COUNT(*) AS count FROM families{where}
            AND EXISTS (
                SELECT 1 FROM unnest(locations) AS value
                WHERE value ILIKE '%remote%'
            )
            """,
            params,
        ).fetchone()["count"]
    return {"companies": companies, "categories": categories, "remote_count": int(remote_count)}


@app.get("/api/families/{family_key}")
def family(family_key: str, trust: str = Query("verified")) -> dict[str, object]:
    if trust not in {"verified", "leads", "all"}:
        raise HTTPException(status_code=400, detail="trust must be verified, leads, or all")
    result = db.get_family(family_key)
    if result is None:
        raise HTTPException(status_code=404, detail="role family not found")
    return _present_family(result, trust=trust)
