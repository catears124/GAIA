from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml


def load_sources() -> dict[str, Any]:
    override = os.getenv("GAIA_SOURCES")
    if override:
        path = Path(override)
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    resource = files("gaia").joinpath("default_sources.yaml")
    return yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
