"use strict";

(() => {
  const RETRY_BASE_MS = 5000;
  const RETRY_MAX_MS = 60000;
  const PROBE_TIMEOUT_MS = 8000;
  const RECOVERY_RELOAD_GUARD_MS = 30000;
  const RELOAD_GUARD_KEY = "gaia:last-recovery-reload";
  let attempts = 0;
  let timer;
  let observer;
  let probeInFlight = false;

  function offline() {
    return document.documentElement.dataset.gaiaOffline === "true" ||
      /could not load jobs|temporarily offline/i.test(document.querySelector("#empty-state")?.textContent || "");
  }

  function paginationNodes() {
    return {
      previous: document.querySelector("#prev"),
      next: document.querySelector("#next"),
      label: document.querySelector("#page-label"),
    };
  }

  function disablePagination() {
    const { previous, next, label } = paginationNodes();
    for (const button of [previous, next]) {
      if (!button) continue;
      if (!button.dataset.gaiaPreofflineDisabled) {
        button.dataset.gaiaPreofflineDisabled = button.disabled ? "true" : "false";
      }
      button.disabled = true;
    }
    if (label) {
      if (!label.dataset.gaiaPreofflineLabel) label.dataset.gaiaPreofflineLabel = label.textContent || "—";
      label.textContent = "Inventory offline";
    }
  }

  function restorePagination() {
    const { previous, next, label } = paginationNodes();
    for (const button of [previous, next]) {
      if (!button) continue;
      if (button.dataset.gaiaPreofflineDisabled) {
        button.disabled = button.dataset.gaiaPreofflineDisabled === "true";
        delete button.dataset.gaiaPreofflineDisabled;
      }
    }
    if (label?.dataset.gaiaPreofflineLabel) {
      label.textContent = label.dataset.gaiaPreofflineLabel;
      delete label.dataset.gaiaPreofflineLabel;
    }
  }

  function liveHealthProbe() {
    return new Promise(resolve => {
      const xhr = new XMLHttpRequest();
      xhr.open("GET", `/api/health?live_probe=${Date.now()}`, true);
      xhr.timeout = PROBE_TIMEOUT_MS;
      xhr.setRequestHeader("Accept", "application/json");
      xhr.setRequestHeader("Cache-Control", "no-store");
      xhr.onload = () => {
        if (xhr.status < 200 || xhr.status >= 300) return resolve(false);
        try {
          const data = JSON.parse(xhr.responseText);
          resolve(data.ok === true && data.inventory?.healthy !== false && data.stale !== true);
        } catch {
          resolve(false);
        }
      };
      xhr.onerror = () => resolve(false);
      xhr.ontimeout = () => resolve(false);
      xhr.send();
    });
  }

  function reloadAfterRecovery() {
    const lastReload = Number(sessionStorage.getItem(RELOAD_GUARD_KEY) || 0);
    if (Date.now() - lastReload < RECOVERY_RELOAD_GUARD_MS) return false;
    sessionStorage.setItem(RELOAD_GUARD_KEY, String(Date.now()));
    location.reload();
    return true;
  }

  function schedule(reset = false, immediate = false) {
    clearTimeout(timer);
    if (reset) attempts = 0;
    if (!offline() || document.hidden) return;
    const delay = immediate ? 0 : Math.min(RETRY_MAX_MS, RETRY_BASE_MS * (2 ** Math.min(attempts, 4)));
    timer = setTimeout(retry, delay);
  }

  async function retry() {
    if (!offline() || document.hidden || probeInFlight) return;
    probeInFlight = true;
    attempts += 1;
    try {
      if (await liveHealthProbe()) {
        document.querySelector("#gaia-emergency-banner")?.remove();
        document.querySelector("#gaia-stale-data-banner")?.remove();
        delete document.documentElement.dataset.gaiaOffline;
        restorePagination();
        window.dispatchEvent(new CustomEvent("gaia:live-data"));
        if (reloadAfterRecovery()) return;
      }
      const button = document.querySelector("[data-retry]");
      if (button && !button.disabled) button.click();
    } finally {
      probeInFlight = false;
      schedule();
    }
  }

  function inspect() {
    const isOffline = offline();
    if (isOffline) {
      disablePagination();
      schedule();
    } else {
      attempts = 0;
      clearTimeout(timer);
      restorePagination();
    }
  }

  function boot() {
    if (observer || !document.body) return;
    observer = new MutationObserver(inspect);
    const empty = document.querySelector("#empty-state");
    if (empty) observer.observe(empty, { childList: true, subtree: true, attributes: true });
    observer.observe(document.body, { childList: true, subtree: false });
    inspect();
  }

  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", boot, { once: true });
  else boot();
  window.addEventListener("online", () => schedule(true, true));
  window.addEventListener("focus", () => schedule(true, true));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) schedule(true, true);
    else clearTimeout(timer);
  });
  window.addEventListener("gaia:live-data", inspect);
  window.addEventListener("gaia:stale-data", inspect);
})();
