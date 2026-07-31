from pathlib import Path


FRONTEND = Path(__file__).parents[1] / "src" / "gaia" / "frontend"


def test_resilience_layer_loads_before_application_fetches() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    resilience = html.index("api-resilience.js")
    application = html.index("app-v2.js")
    assert resilience < application


def test_resilience_layer_only_intercepts_safe_api_reads() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert 'method !== "GET"' in script
    assert 'url.origin !== location.origin' in script
    assert 'response.status >= 500' in script
    assert 'X-GAIA-Stale' in script


def test_cached_inventory_is_explicitly_disclosed() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert "Showing the most recent cached inventory" in script
    assert 'role", "status"' in script
