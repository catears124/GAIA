from pathlib import Path


FRONTEND = Path(__file__).parents[1] / "src" / "gaia" / "frontend"


def test_resilience_layer_loads_before_application_fetches() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    resilience = html.index("api-resilience.js")
    application = html.index("app-v2.js")
    assert resilience < application
    assert 'api-resilience.js?v=2.0.0' in html


def test_resilience_layer_only_intercepts_safe_api_reads() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert 'method !== "GET"' in script
    assert 'url.origin !== location.origin' in script
    assert 'response.status >= 500' in script
    assert 'X-GAIA-Stale' in script


def test_cached_inventory_is_explicitly_disclosed() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert "Showing cached inventory" in script
    assert 'role", "status"' in script
    assert 'aria-live", "polite"' in script


def test_stale_cache_is_bounded_and_pruned() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert "MAX_STALE_MS" in script
    assert "MAX_CACHE_ENTRIES" in script
    assert "cache.delete(request)" in script
    assert "entries.slice(MAX_CACHE_ENTRIES)" in script


def test_cached_health_can_never_look_live() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert 'url?.pathname !== "/api/health"' in script
    assert "payload.ok = false" in script
    assert "stale_snapshot: true" in script
    assert "healthy: false" in script


def test_live_recovery_removes_stale_banner() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert "clearStaleBanner()" in script
    assert 'new CustomEvent("gaia:live-data")' in script


def test_snapshot_supports_arbitrary_offline_family_searches() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert "filterFamilyIndex" in script
    assert "offlineFamilies" in script
    assert "offlineFacets" in script
    assert 'url.pathname === "/api/families"' in script
    assert 'url.pathname === "/api/facets"' in script
    assert 'url.searchParams.get("q")' in script
    assert 'url.searchParams.get("location")' in script
    assert 'url.searchParams.get("company")' in script
    assert 'url.searchParams.get("posted_within")' in script
    assert 'url.searchParams.get("sort")' in script
    assert 'headers.set("X-GAIA-Offline-Search", "1")' in script


def test_offline_search_preserves_truthful_trust_and_cycle_filters() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert 'trust === "verified" && !item.verified' in script
    assert 'trust === "leads" && item.verified' in script
    assert 'target === "exact"' in script
    assert 'year === 2027 && season === "summer"' in script
    assert 'target === "default" || target === "year_confirmed"' in script
