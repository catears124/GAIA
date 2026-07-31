"""Vercel entrypoint for the GAIA FastAPI application.

The public deployment reads PostgreSQL through Supabase's transaction pooler.
A recreated empty Supabase project is initialized through POSTGRES_URL_NON_POOLING
and repopulated from GAIA's bundled public registries on the first cold start.
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
    # Vercel owns the only current database credentials after a Supabase recreation.
    # Runtime migration is fingerprinted, advisory-locked, and uses the non-pooling URL.
    os.environ.setdefault("GAIA_AUTO_MIGRATE", "1")
    os.environ.setdefault("GAIA_BOOTSTRAP_EMPTY_DATABASE", "1")
    os.environ.setdefault("GAIA_BOOTSTRAP_BUDGET_SECONDS", "38")
    os.environ.setdefault("GAIA_CANDIDATE_PROBE_LIMIT", "6")
    # Public reads fail closed quickly during provider recovery.
    os.environ.setdefault("GAIA_DB_TIMEOUT", "8")

from gaia.api_resilience import install_database_outage_guard  # noqa: E402
from gaia.product_api import app  # noqa: E402,F401
from gaia.runtime_bootstrap import install_runtime_bootstrap  # noqa: E402

install_runtime_bootstrap(app)
install_database_outage_guard(app)
