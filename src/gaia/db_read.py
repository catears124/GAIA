from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .db_base import TARGET_MATCHES, application_identity, coverage_role_signature, iso
from .quality import TECH_CATEGORIES


class ReadMixin:
    def list_families(
        self,
        *,
        query: str = "",
        category: str = "",
        target: str = "default",
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, object]:
        conditions: list[str] = []
        params: list[object] = []
        if target == "default":
            conditions.append("target_match = ANY(%s)")
            params.append(list(TARGET_MATCHES))
        elif target:
            conditions.append("target_match=%s")
            params.append(target)
        if category:
            conditions.append("category=%s")
            params.append(category)
        if query:
            conditions.append(
                "(company ILIKE %s OR title ILIKE %s OR array_to_string(locations, ' ') ILIKE %s)"
            )
            needle = f"%{query}%"
            params.extend([needle, needle, needle])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        offset = max(0, page - 1) * page_size
        with self.connect() as db:
            total_row = db.execute(
                f"SELECT COUNT(*) AS count FROM families{where}", params
            ).fetchone()
            rows = db.execute(
                f"""
                SELECT * FROM families{where}
                ORDER BY COALESCE(latest_posted_at, first_detected_at) DESC
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, offset],
            ).fetchall()
        return {
            "total": int(total_row["count"]),
            "items": [self._family_dict(row) for row in rows],
        }

    def get_family(self, key: str) -> dict[str, object] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM families WHERE family_key=%s", (key,)
            ).fetchone()
        return self._family_dict(row) if row else None

    def coverage(self) -> dict[str, object]:
        with self.connect() as db:
            latest_row = db.execute(
                "SELECT MAX(id) AS id FROM sync_runs WHERE finished_at IS NOT NULL"
            ).fetchone()
            latest_run = latest_row["id"]
            if latest_run is None:
                latest_row = db.execute(
                    "SELECT MAX(last_run_id) AS id FROM source_health"
                ).fetchone()
                latest_run = latest_row["id"]
            if latest_run is None:
                health_rows = db.execute(
                    "SELECT * FROM source_health ORDER BY source"
                ).fetchall()
            else:
                health_rows = db.execute(
                    "SELECT * FROM source_health WHERE last_run_id=%s ORDER BY source",
                    (latest_run,),
                ).fetchall()
            health = [self._json_row(row) for row in health_rows]
            benchmark_row = db.execute(
                """
                SELECT COUNT(*) AS count FROM benchmark_cases
                WHERE version=(SELECT MAX(version) FROM benchmark_cases)
                """
            ).fetchone()
            benchmark_size = int(benchmark_row["count"])
            family_counts = dict(
                db.execute(
                    """
                    SELECT
                        COUNT(*) AS families,
                        COUNT(DISTINCT company) AS companies,
                        COUNT(*) FILTER (WHERE direct_openings > 0) AS direct_families,
                        COUNT(*) FILTER (
                            WHERE direct_openings = 0 AND backstop_openings > 0
                        ) AS backstop_only
                    FROM families
                    WHERE target_match = ANY(%s)
                      AND category = ANY(%s)
                    """,
                    (list(TARGET_MATCHES), list(TECH_CATEGORIES)),
                ).fetchone()
            )
            posting_rows = db.execute(
                """
                SELECT canonical_apply_url, source, source_id, source_mode, company,
                       normalized_title, posted_at
                FROM postings
                WHERE active
                  AND target_match = ANY(%s)
                  AND category = ANY(%s)
                """,
                (list(TARGET_MATCHES), list(TECH_CATEGORIES)),
            ).fetchall()

        identities: dict[str, set[str]] = {
            "registry": set(),
            "direct": set(),
            "verification": set(),
            "external-index": set(),
        }
        identity_roles: dict[str, dict[str, set[str]]] = {mode: {} for mode in identities}
        companies_by_mode = {mode: set() for mode in identities}
        productive_direct_sources: set[str] = set()
        dated_direct_applications: set[str] = set()
        for row in posting_rows:
            mode = str(row["source_mode"])
            if mode not in identities:
                continue
            identity = application_identity(
                str(row["canonical_apply_url"]),
                str(row["source"]),
                str(row["source_id"]),
            )
            identities[mode].add(identity)
            if mode == "direct":
                productive_direct_sources.add(str(row["source"]))
                if row["posted_at"]:
                    dated_direct_applications.add(identity)
            role = coverage_role_signature(str(row["company"]), str(row["normalized_title"]))
            identity_roles[mode].setdefault(identity, set()).add(role)
            companies_by_mode[mode].add(str(row["company"]))

        registry_floor = identities["registry"]
        direct_roles = set().union(*identity_roles["direct"].values())
        independent_roles = direct_roles | set().union(*identity_roles["verification"].values())
        direct_matches = {
            identity
            for identity in registry_floor
            if identity in identities["direct"]
            or bool(identity_roles["registry"][identity] & direct_roles)
        }
        independent_matches = {
            identity
            for identity in registry_floor
            if identity in identities["direct"] | identities["verification"]
            or bool(identity_roles["registry"][identity] & independent_roles)
        }
        registry_only = registry_floor - independent_matches
        registry_roles = set().union(*identity_roles["registry"].values())
        direct_only = {
            identity
            for identity in identities["direct"]
            if not identity_roles["direct"][identity] & registry_roles
        }
        mode_counts = Counter(str(row["mode"]) for row in health)
        status_counts = Counter(str(row.get("status") or "unknown") for row in health)

        current_sources = [row for row in health if str(row.get("scope") or "current") == "current"]
        historical_sources = [row for row in health if str(row.get("scope")) == "historical"]
        complete_enumerators = sum(
            bool(row["complete"])
            and str(row["mode"]) == "board"
            and str(row.get("status")) == "ok"
            for row in current_sources
        )
        historical_enumerators = sum(
            bool(row["complete"])
            and str(row["mode"]) == "board"
            and str(row.get("status")) == "ok"
            for row in historical_sources
        )

        def has_note(row: dict[str, object], phrase: str) -> bool:
            return phrase in str(row.get("note") or "")

        actionable = [
            row
            for row in current_sources
            if row.get("last_error")
            or str(row.get("status")) in {"broken", "truncated", "empty"}
        ]
        access_limited = [
            row
            for row in current_sources
            if str(row.get("status")) == "blocked" or has_note(row, "access-blocked")
        ]
        stale_verifications = [
            row
            for row in current_sources
            if str(row["mode"]) == "verification"
            and (str(row.get("status")) == "stale" or has_note(row, "stale/closed"))
        ]
        unstructured_verifications = [
            row
            for row in current_sources
            if str(row["mode"]) == "verification"
            and (
                str(row.get("status")) == "unstructured"
                or has_note(row, "without JobPosting")
            )
        ]
        dormant_watches = [
            row
            for row in historical_sources
            if str(row.get("status")) in {"dormant", "empty", "stale"}
        ]
        historical_failures = [row for row in historical_sources if row.get("last_error")]
        truncated = [
            row
            for row in current_sources
            if row["expected_rows"] is not None
            and int(row["rows_scanned"] or 0) < int(row["expected_rows"])
            and str(row["mode"]) == "board"
        ]
        registry_recall = (
            round(100 * len(independent_matches) / len(registry_floor), 1)
            if registry_floor
            else None
        )

        return {
            "summary": {
                **{key: int(value or 0) for key, value in family_counts.items()},
                "known_applications": len(set().union(*identities.values())),
                "registry_floor": len(registry_floor),
                "direct_applications": len(identities["direct"]),
                "verified_applications": len(identities["verification"]),
                "productive_direct_sources": len(productive_direct_sources),
                "direct_date_coverage_percent": round(
                    100 * len(dated_direct_applications) / len(identities["direct"]), 1
                )
                if identities["direct"]
                else None,
                "direct_matches": len(direct_matches),
                "independent_matches": len(independent_matches),
                "registry_only": len(registry_only),
                "direct_only": len(direct_only),
                "registry_recall_percent": registry_recall,
                "benchmark_size": benchmark_size,
            },
            "contract": {
                "run_id": latest_run,
                "configured_sources": len(health),
                "current_sources": len(current_sources),
                "historical_sources": len(historical_sources),
                "complete_enumerators": complete_enumerators,
                "historical_enumerators": historical_enumerators,
                "actionable_anomalies": len(actionable),
                "access_limited": len(access_limited),
                "stale_verifications": len(stale_verifications),
                "unstructured_verifications": len(unstructured_verifications),
                "dormant_watches": len(dormant_watches),
                "historical_failures": len(historical_failures),
                "truncated_sources": len(truncated),
                "modes": dict(mode_counts),
                "statuses": dict(status_counts),
                "companies_by_mode": {
                    mode: len(companies) for mode, companies in companies_by_mode.items()
                },
            },
            "sources": health,
        }

    @staticmethod
    def _json_row(row: Mapping[str, Any]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in row.items():
            result[key] = iso(value) if isinstance(value, datetime) else value
        return result

    @classmethod
    def _family_dict(cls, row: Mapping[str, Any]) -> dict[str, object]:
        result = cls._json_row(row)
        result["locations"] = list(result.get("locations") or [])
        result["openings"] = list(result.get("openings") or [])
        return result
