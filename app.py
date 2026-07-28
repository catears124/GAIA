"""Vercel entrypoint for the GAIA FastAPI application.

The public deployment reads PostgreSQL through Supabase's transaction pooler.
The continuous crawler runs in a separate worker process and writes to the same database.
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

from gaia.product_api import app  # noqa: E402,F401
