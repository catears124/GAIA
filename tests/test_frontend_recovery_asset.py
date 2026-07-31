from pathlib import Path


FRONTEND = Path(__file__).parents[1] / "src" / "gaia" / "frontend"


def test_index_loads_outage_controller_after_application_runtime() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    controller = '/assets/outage-controller.js?v=1.2.1'
    app = '/assets/app-v2.js?v=8.1.1'
    assert controller in html
    assert html.index(app) < html.index(controller)


def test_outage_controller_has_uncached_live_probe_and_backoff() -> None:
    script = (FRONTEND / "outage-controller.js").read_text(encoding="utf-8")
    assert "XMLHttpRequest" in script
    assert "live_probe=${Date.now()}" in script
    assert 'setRequestHeader("Cache-Control", "no-store")' in script
    assert "RETRY_MAX_MS = 60000" in script
    assert "document.hidden" in script
    assert "location.reload()" in script


def test_recovery_requires_fresh_nonempty_inventory() -> None:
    script = (FRONTEND / "outage-controller.js").read_text(encoding="utf-8")
    assert "inventoryIsFresh" in script
    assert "total <= 0" in script
    assert "MAX_HEALTH_AGE_MS" in script
    assert "Date.parse(generatedAt)" in script
    assert "age >= -5 * 60 * 1000" in script
    assert "data.stale !== true" in script


def test_recovery_status_is_announced_accessibly() -> None:
    script = (FRONTEND / "outage-controller.js").read_text(encoding="utf-8")
    assert 'status.setAttribute("role", "status")' in script
    assert 'status.setAttribute("aria-live", "polite")' in script
    assert 'status.setAttribute("aria-atomic", "true")' in script
    assert "Live internship inventory recovered" in script
