from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "src" / "gaia" / "frontend"


def test_resilience_layer_loads_before_application_fetches() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    remote = html.index("remote-snapshot.js")
    resilience = html.index("api-resilience.js")
    detail = html.index("offline-family-detail.js")
    application = html.index("app-v2.js")
    assert remote < resilience < detail < application
    assert 'remote-snapshot.js?v=1.0.2' in html
    assert 'api-resilience.js?v=2.1.0' in html
    assert 'offline-family-detail.js?v=1.0.0' in html
    assert 'outage-controller.js?v=1.2.2' in html


def test_resilience_layer_only_intercepts_safe_api_reads() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert 'method !== "GET"' in script
    assert 'url.origin !== location.origin' in script
    assert 'response.status >= 500' in script
    assert 'X-GAIA-Stale' in script


def test_live_api_requests_have_a_hard_deadline() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert "LIVE_TIMEOUT_MS = 7000" in script
    assert "fetchWithDeadline" in script
    assert "new AbortController()" in script
    assert "GAIA request exceeded" in script
    assert "if (request.signal?.aborted) throw error" in script


def test_published_snapshot_is_refreshable_and_preferred_over_device_cache() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert "SNAPSHOT_TIMEOUT_MS = 6500" in script
    assert "SNAPSHOT_REFRESH_MS = 60 * 1000" in script
    assert "staticSnapshotFetchedAt" in script
    assert "now - staticSnapshotFetchedAt >= SNAPSHOT_REFRESH_MS" in script
    assert "return await staticSnapshotResponse(request) || await cachedResponse(request)" in script


def test_remote_snapshot_transport_has_its_own_deadline() -> None:
    script = (FRONTEND / "remote-snapshot.js").read_text(encoding="utf-8")
    assert "FETCH_TIMEOUT_MS = 6000" in script
    assert "fetchWithDeadline" in script
    assert "Snapshot fetch timed out" in script
    assert "sourceSignal?.aborted" in script


def test_cached_inventory_is_explicitly_disclosed() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert 'source === "snapshot" ? "last deployed inventory" : "cached inventory"' in script
    assert "Live database unavailable. Showing ${kind}" in script
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


def test_offline_search_mirrors_canonical_trust_cycle_and_token_filters() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert 'new Set(["exact", "year_confirmed", "source_confirmed"])' in script
    assert 'target === "default"' in script
    assert "TARGET_MATCHES.has(match)" in script
    assert "return match === target" in script
    assert 'trust === "verified" && !item.verified' in script
    assert 'trust === "leads" && item.verified' in script
    assert "queryTokens.some(token => !haystack.includes(token))" in script
    assert 'String(item.company || "").toLowerCase() !== company.toLowerCase()' in script


def test_offline_facets_preserve_categories_and_remote_counts() -> None:
    script = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    assert "categoryCounts" in script
    assert "remoteCount" in script
    assert "categories: ranked(categoryCounts)" in script
    assert "remote_count: remoteCount" in script


def test_family_drawer_uses_snapshot_details_when_live_api_is_down() -> None:
    script = (FRONTEND / "offline-family-detail.js").read_text(encoding="utf-8")
    assert r'^\/api\/families\/([^/]+)$' in script
    assert "snapshot?.family_index" in script
    assert "candidate?.family_key === request.key" in script
    assert 'trust === "verified"' in script
    assert 'trust === "leads"' in script
    assert 'opening?.source_mode || ""' in script
    assert '"X-GAIA-Offline-Detail": "1"' in script
    assert "copy.openings = openings.filter" in script


def test_outage_controller_probes_live_health_without_fetch_fallbacks() -> None:
    script = (FRONTEND / "outage-controller.js").read_text(encoding="utf-8")
    assert "new XMLHttpRequest()" in script
    assert 'xhr.open("GET", `/api/health?live_probe=${Date.now()}`' in script
    assert "PROBE_TIMEOUT_MS" in script
    assert "data.ok === true" in script
    assert "data.inventory?.healthy !== false" in script
    assert "data.stale !== true" in script


def test_outage_controller_accepts_real_inventory_activity_timestamp() -> None:
    script = (FRONTEND / "outage-controller.js").read_text(encoding="utf-8")
    assert "data.inventory?.latest_activity_at" in script
    assert "data.data?.last_success_at" in script
    assert "MAX_HEALTH_AGE_MS" in script


def test_outage_controller_restores_pagination_after_recovery() -> None:
    script = (FRONTEND / "outage-controller.js").read_text(encoding="utf-8")
    assert "gaiaPreofflineDisabled" in script
    assert "gaiaPreofflineLabel" in script
    assert "restorePagination()" in script
    assert "Inventory offline" in script


def test_recovery_reload_is_guarded_against_loops() -> None:
    script = (FRONTEND / "outage-controller.js").read_text(encoding="utf-8")
    assert "RECOVERY_RELOAD_GUARD_MS" in script
    assert 'RELOAD_GUARD_KEY = "gaia:last-recovery-reload"' in script
    assert "sessionStorage.setItem" in script
    assert "location.reload()" in script


def test_cached_summary_is_never_styled_as_healthy() -> None:
    script = (FRONTEND / "app-improvements.js").read_text(encoding="utf-8")
    assert "truthfullyHealthy = !cached" in script
    assert "health.ok === true" in script
    assert "health.stale !== true" in script
    assert "total > 0" in script
    assert 'node.classList.add(truthfullyHealthy ? "fresh" : "stale")' in script
    assert "cached snapshot" in script


def test_summary_requests_are_bounded_and_non_overlapping() -> None:
    script = (FRONTEND / "app-improvements.js").read_text(encoding="utf-8")
    assert "REQUEST_TIMEOUT_MS = 8000" in script
    assert "new AbortController()" in script
    assert "setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)" in script
    assert "if (recoveryInFlight) return recoveryInFlight" in script
    assert "scheduleRecovery" in script
    assert "setInterval(recoverLiveSummary" not in script


def test_hidden_tabs_suspend_summary_polling_and_resume_immediately() -> None:
    script = (FRONTEND / "app-improvements.js").read_text(encoding="utf-8")
    assert 'document.addEventListener("visibilitychange"' in script
    assert "if (document.hidden) clearTimeout(recoveryTimer)" in script
    assert 'window.addEventListener("online", recoverLiveSummary)' in script


def test_outage_controller_is_loaded_at_most_once() -> None:
    script = (FRONTEND / "app-improvements.js").read_text(encoding="utf-8")
    assert 'script.src.includes("/assets/outage-controller.js")' in script
    assert "if (alreadyLoaded) return" in script


def test_saved_export_always_restores_button_state() -> None:
    script = (FRONTEND / "app-improvements.js").read_text(encoding="utf-8")
    assert "try {" in script
    assert "finally {" in script
    assert 'button.textContent = "Export CSV"' in script
