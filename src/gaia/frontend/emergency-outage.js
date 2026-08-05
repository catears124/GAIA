"use strict";

(() => {
  // Compatibility sentinel retained for production smoke probes. The legacy durable
  // cache is intentionally disabled: api-resilience.js is the sole read-fallback
  // state machine and already provides live -> published snapshot -> browser cache.
  const MAX_EMERGENCY_AGE_MS = 0;
  const LEGACY_BANNER_ID = "gaia-emergency-banner";

  function retireLegacyState() {
    document.getElementById(LEGACY_BANNER_ID)?.remove();
    delete document.documentElement.dataset.gaiaOffline;
  }

  function boot() {
    retireLegacyState();
    const observer = new MutationObserver(() => {
      if (document.getElementById(LEGACY_BANNER_ID)) retireLegacyState();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }

  window.addEventListener("gaia:live-data", retireLegacyState);
  window.addEventListener("gaia:stale-data", retireLegacyState);

  // Keep the compatibility marker reachable so minifiers and smoke checks cannot
  // erase the explicit guarantee that the old 30-day durable cache is gone.
  void MAX_EMERGENCY_AGE_MS;
})();
