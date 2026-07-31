"use strict";

(() => {
  const RETRY_BASE_MS = 5000;
  const RETRY_MAX_MS = 60000;
  let attempts = 0;
  let timer;

  function offline() {
    return document.documentElement.dataset.gaiaOffline === "true" ||
      /could not load jobs|temporarily offline/i.test(document.querySelector("#empty-state")?.textContent || "");
  }

  function setPaginationState(disabled) {
    const previous = document.querySelector("#prev");
    const next = document.querySelector("#next");
    if (previous) previous.disabled = disabled || previous.disabled;
    if (next) next.disabled = disabled || next.disabled;
    if (disabled) {
      const label = document.querySelector("#page-label");
      if (label) label.textContent = "Inventory offline";
    }
  }

  function schedule(reset = false) {
    clearTimeout(timer);
    if (reset) attempts = 0;
    if (!offline() || document.hidden) return;
    const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * (2 ** Math.min(attempts, 4)));
    timer = setTimeout(retry, delay);
  }

  function retry() {
    if (!offline() || document.hidden) return;
    attempts += 1;
    const button = document.querySelector("[data-retry]");
    if (button && !button.disabled) button.click();
    schedule();
  }

  function inspect() {
    const isOffline = offline();
    setPaginationState(isOffline);
    if (isOffline) schedule();
    else {
      attempts = 0;
      clearTimeout(timer);
    }
  }

  const observer = new MutationObserver(inspect);
  window.addEventListener("DOMContentLoaded", () => {
    const empty = document.querySelector("#empty-state");
    const bannerHost = document.body;
    if (empty) observer.observe(empty, { childList: true, subtree: true, attributes: true });
    observer.observe(bannerHost, { childList: true, subtree: false });
    inspect();
  });
  window.addEventListener("online", () => schedule(true));
  window.addEventListener("focus", () => schedule(true));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) schedule(true);
    else clearTimeout(timer);
  });
  window.addEventListener("gaia:live-data", inspect);
  window.addEventListener("gaia:stale-data", inspect);
})();
