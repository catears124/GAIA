"""Vercel entrypoint for the GAIA FastAPI application.

The build imports only the read application. Schema initialization and empty-database
recovery are loaded lazily by the ASGI startup hook, where runtime credentials and
network access are valid.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

if os.getenv("VERCEL"):
    os.environ.setdefault("GAIA_INITIAL_SYNC", "0")
    os.environ.setdefault("GAIA_READ_ONLY", "1")
    os.environ.setdefault("GAIA_AUTO_MIGRATE", "0")
    os.environ.setdefault("GAIA_BOOTSTRAP_EMPTY_DATABASE", "1")
    os.environ.setdefault("GAIA_BOOTSTRAP_BUDGET_SECONDS", "38")
    os.environ.setdefault("GAIA_CANDIDATE_PROBE_LIMIT", "6")
    os.environ.setdefault("GAIA_DB_TIMEOUT", "8")

from gaia.api_resilience import install_database_outage_guard  # noqa: E402
from gaia.product_api import app  # noqa: E402,F401


async def _runtime_bootstrap() -> None:
    # Keep crawler, discovery, and migration imports out of Vercel's build-time ASGI
    # validation. They are needed only after a production function actually starts.
    from gaia.runtime_bootstrap import bootstrap_empty_database

    await bootstrap_empty_database()


app.add_event_handler("startup", _runtime_bootstrap)
install_database_outage_guard(app)
