from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "src" / "gaia" / "frontend"


def test_emergency_outage_has_bounded_durable_fallback():
    script = (FRONTEND / "emergency-outage.js").read_text(encoding="utf-8")
    assert 'LOCAL_KEY = "gaia:emergency-api-v1"' in script
    assert "MAX_LOCAL_ENTRIES = 48" in script
    assert "MAX_LOCAL_BYTES = 3_500_000" in script
    assert "MAX_EMERGENCY_AGE_MS = 30 * 24 * 60 * 60 * 1000" in script
    assert "REQUEST_DEADLINE_MS = 9000" in script
    assert "localStorage.setItem(LOCAL_KEY" in script
    assert "Promise.race([promise, deadline])" in script


def test_emergency_health_never_reports_cached_inventory_healthy():
    script = (FRONTEND / "emergency-outage.js").read_text(encoding="utf-8")
    assert "data.ok = false" in script
    assert "data.healthy = false" in script
    assert "data.stale = true" in script
    assert "healthy: false, stale_snapshot: true" in script
    assert "X-GAIA-Durable-Backup" in script


def test_emergency_wrapper_never_intercepts_writes():
    script = (FRONTEND / "emergency-outage.js").read_text(encoding="utf-8")
    assert 'method !== "GET"' in script
    assert "url.origin !== location.origin" in script
    assert 'READ_ENDPOINTS = new Set(["health", "stats", "facets", "families", "coverage", "universe"])' in script


def test_outage_controller_retries_with_backoff_and_stops_when_hidden():
    script = (FRONTEND / "outage-controller.js").read_text(encoding="utf-8")
    assert "RETRY_BASE_MS = 5000" in script
    assert "RETRY_MAX_MS = 60000" in script
    assert "2 ** Math.min(attempts, 4)" in script
    assert "document.hidden" in script
    assert 'window.addEventListener("online"' in script
    assert 'window.addEventListener("focus"' in script
    assert "button.click()" in script
    assert 'label.textContent = "Inventory offline"' in script
