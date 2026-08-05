from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "src" / "gaia" / "frontend"


def test_legacy_emergency_layer_cannot_cache_or_intercept_requests() -> None:
    script = (FRONTEND / "emergency-outage.js").read_text(encoding="utf-8")

    assert "window.fetch =" not in script
    assert "Promise.race" not in script
    assert "localStorage" not in script
    assert "X-GAIA-Durable-Backup" not in script
    assert "MAX_EMERGENCY_AGE_MS = 0" in script


def test_legacy_offline_banner_is_removed_defensively() -> None:
    script = (FRONTEND / "emergency-outage.js").read_text(encoding="utf-8")

    assert 'LEGACY_BANNER_ID = "gaia-emergency-banner"' in script
    assert "document.getElementById(LEGACY_BANNER_ID)?.remove()" in script
    assert "MutationObserver" in script
    assert 'window.addEventListener("gaia:live-data"' in script
    assert 'window.addEventListener("gaia:stale-data"' in script


def test_outage_controller_retries_with_backoff_and_stops_when_hidden() -> None:
    script = (FRONTEND / "outage-controller.js").read_text(encoding="utf-8")
    assert "RETRY_BASE_MS = 5000" in script
    assert "RETRY_MAX_MS = 60000" in script
    assert "2 ** Math.min(attempts, 4)" in script
    assert "document.hidden" in script
    assert 'window.addEventListener("online"' in script
    assert 'window.addEventListener("focus"' in script
    assert "button.click()" in script
    assert 'label.textContent = "Inventory offline"' in script
