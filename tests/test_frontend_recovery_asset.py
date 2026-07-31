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
