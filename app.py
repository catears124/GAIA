"""Vercel entrypoint for the GAIA FastAPI application.

Vercel functions have ephemeral writable storage. A compact, versioned SQLite
snapshot is copied into /tmp on cold start so every deployment opens with a
useful index while the crawler remains a separate, stateful process.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "deploy" / "gaia-snapshot.db"
sys.path.insert(0, str(ROOT / "src"))


def _runtime_database() -> Path:
    digest = hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()[:12]
    destination = Path(tempfile.gettempdir()) / f"gaia-{digest}.db"
    if not destination.exists():
        shutil.copyfile(SNAPSHOT, destination)
    return destination


if os.getenv("VERCEL"):
    if not SNAPSHOT.exists():
        raise RuntimeError("deploy/gaia-snapshot.db is missing; build the deployment snapshot")
    os.environ.setdefault("GAIA_DB", str(_runtime_database()))
    os.environ.setdefault("GAIA_INITIAL_SYNC", "0")
    os.environ.setdefault("GAIA_READ_ONLY", "1")

from gaia.api import app  # noqa: E402,F401
