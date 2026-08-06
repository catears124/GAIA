from __future__ import annotations

import json
import os
import sys
from typing import Any

from .db import Database

_SECRET_NAMES = ("VERIFIED_DHOOK", "LEADS_DHOOK")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gaia_runtime_secrets (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE gaia_runtime_secrets ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE gaia_runtime_secrets FROM anon, authenticated;
"""


def ensure_runtime_secret_store(database: Database) -> None:
    with database.connect() as connection:
        connection.execute(_SCHEMA)


def sync_runtime_secrets(database: Database | None = None) -> dict[str, object]:
    database = database or Database(migrate=False)
    configured = {
        name: os.getenv(name, "").strip()
        for name in _SECRET_NAMES
        if os.getenv(name, "").strip()
    }
    ensure_runtime_secret_store(database)
    with database.connect() as connection:
        for name, value in configured.items():
            connection.execute(
                """
                INSERT INTO gaia_runtime_secrets(name, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT(name) DO UPDATE
                SET value=EXCLUDED.value,
                    updated_at=now()
                """,
                (name, value),
            )
    return {
        "configured": sorted(configured),
        "missing": sorted(set(_SECRET_NAMES) - set(configured)),
    }


def load_runtime_secret(database: Database, name: str) -> str:
    if name not in _SECRET_NAMES:
        return ""
    try:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM gaia_runtime_secrets WHERE name=%s",
                (name,),
            ).fetchone()
    except Exception:
        return ""
    return str(dict(row or {}).get("value") or "").strip()


def resolved_runtime_secret(database: Database, name: str) -> str:
    return os.getenv(name, "").strip() or load_runtime_secret(database, name)


def main() -> int:
    try:
        result: dict[str, Any] = sync_runtime_secrets()
    except Exception as error:  # noqa: BLE001 - CLI must surface infrastructure failure.
        print(f"Runtime secret synchronization failed: {error!r}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
