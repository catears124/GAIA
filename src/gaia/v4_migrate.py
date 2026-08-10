from __future__ import annotations

from copy import deepcopy
from typing import Any


def _legacy_family_verified(family: dict[str, Any]) -> bool:
    if bool(family.get("verified")):
        return True
    try:
        if int(family.get("direct_openings") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return str(family.get("quality") or "").casefold() in {"employer", "verified", "direct"}


def sanitize_previous_snapshot(snapshot: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Create a safe v4 migration baseline from a pre-v4 snapshot.

    Legacy snapshots predate the explicit evidence contract. In particular, an
    opening with no ``source_mode`` cannot safely be interpreted as direct merely
    because the old dataclass default was ``direct``. That mistake turns historical
    leads into fake verified jobs during migration.

    For a pre-v4 snapshot we retain only families that the *old family-level
    contract itself* already regarded as employer/direct. All old leads are rebuilt
    from current v4 market sensors instead of being granted accidental trust.
    """
    try:
        schema = int(snapshot.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema = 0
    if schema >= 4:
        return snapshot, False

    migrated = deepcopy(snapshot)
    families = [
        family
        for family in migrated.get("family_index") or []
        if isinstance(family, dict) and _legacy_family_verified(family)
    ]
    for family in families:
        family["verified"] = True
        family["quality"] = "employer"
        for opening in family.get("openings") or []:
            if not isinstance(opening, dict):
                continue
            mode = str(opening.get("source_mode") or "").casefold()
            source = str(opening.get("source") or "").casefold()
            if mode in {"registry", "lead", "external-index", "verification-lead", "market-sensor"}:
                continue
            if source.startswith(("registry:", "sensor:")):
                opening["source_mode"] = "market-sensor"
            elif not mode:
                # The family was already certified direct by the old contract, so
                # an omitted per-opening mode may safely inherit direct here only.
                opening["source_mode"] = "direct"

    migrated["family_index"] = families
    migrated["family_index_total"] = len(families)
    migrated["migration"] = {
        "from_schema_version": schema,
        "legacy_families_retained": len(families),
        "policy": "retain only legacy family-level verified/direct evidence; rebuild leads from v4 sensors",
    }
    return migrated, True
