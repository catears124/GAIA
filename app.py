"""Vercel entrypoint for the GAIA FastAPI application.

The public deployment reads PostgreSQL through Supabase's transaction pooler.
A recreated empty Supabase project is initialized during ASGI startup, never during
Vercel's build-time import check, and then repopulated from bundled public registries.
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
    # Vercel imports the ASGI module while building. Real network/database work belongs
    # exclusively to the startup handler, which is advisory-locked and fingerprinted.
    os.environ.setdefault("GAIA_AUTO_MIGRATE", "0")
    os.environ.setdefault("GAIA_BOOTSTRAP_EMPTY_DATABASE", "1")
    os.environ.setdefault("GAIA_BOOTSTRAP_BUDGET_SECONDS", "38")
    os.environ.setdefault("GAIA_CANDIDATE_PROBE_LIMIT", "6")
    os.environ.setdefault("GAIA_DB_TIMEOUT", "8")

from gaia.api_resilience import install_database_outage_guard  # noqa: E402
from gaia.product_api import app  # noqa: E402,F401
from gaia.runtime_bootstrap import install_runtime_bootstrap  # noqa: E402

install_runtime_bootstrap(app)
install_database_outage_guard(app)
