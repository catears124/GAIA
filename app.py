"""Vercel entrypoint for the GAIA FastAPI application.

The build imports only lightweight application code. Schema initialization and
empty-database recovery begin on the first real API request. Ongoing inventory ticks
also run inside Vercel, so a recreated Supabase project does not depend on stale
GitHub database credentials.
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
    os.environ.setdefault("GAIA_ENABLE_RUNTIME_TICK", "1")
    os.environ.setdefault("GAIA_RUNTIME_TICK_INTERVAL_SECONDS", "600")
    os.environ.setdefault("GAIA_RUNTIME_TICK_BUDGET_SECONDS", "42")
    os.environ.setdefault("GAIA_RUNTIME_TICK_CONCURRENCY", "6")
    os.environ.setdefault("GAIA_DB_TIMEOUT", "8")

from gaia.api_resilience import install_database_outage_guard  # noqa: E402
from gaia.coverage_api import install_coverage_api  # noqa: E402
from gaia.maintenance_api import install_maintenance_api  # noqa: E402
from gaia.product_api import app  # noqa: E402,F401
from gaia.request_bootstrap import install_request_bootstrap  # noqa: E402

install_request_bootstrap(app)
install_coverage_api(app)
install_maintenance_api(app)
install_database_outage_guard(app)
