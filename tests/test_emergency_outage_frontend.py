from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "src" / "gaia" / "frontend"


def test_emergency_outage_runtime_loads_before_primary_resilience() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    emergency = html.index("/assets/emergency-outage.js")
    primary = html.index("/assets/api-resilience.js")
    app = html.index("/assets/app-v2.js")
    assert emergency < primary < app


def test_emergency_cache_is_bounded_and_truthful() -> None:
    source = (FRONTEND / "emergency-outage.js").read_text(encoding="utf-8")
    assert "30 * 24 * 60 * 60 * 1000" in source
    assert 'headers.set("X-GAIA-Stale", "1")' in source
    assert 'data.ok = false' in source
    assert 'data.inventory = { ...(data.inventory || {}), healthy: false' in source
    assert "Live inventory is offline" in source


def test_emergency_runtime_never_intercepts_writes() -> None:
    source = (FRONTEND / "emergency-outage.js").read_text(encoding="utf-8")
    assert 'method !== "GET"' in source
    assert "READ_ENDPOINTS" in source
