"use strict";

(() => {
  const nativeFetch = window.fetch.bind(window);
  const LOCAL_PATH = "/assets/last-known-inventory.json";
  const REMOTE_URL = "https://raw.githubusercontent.com/catears124/GAIA/snapshot-data/src/gaia/frontend/last-known-inventory.json";
  const SNAPSHOT_BANNER_ID = "gaia-stale-data-banner";
  const FETCH_TIMEOUT_MS = 6000;

  function requestUrl(input) {
    try { return new URL(input instanceof Request ? input.url : input, location.href); }
    catch { return null; }
  }

  function isSnapshotRequest(input, init = {}) {
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const url = requestUrl(input);
    return method === "GET" && url?.origin === location.origin && url.pathname === LOCAL_PATH;
  }

  function ageLabel(value) {
    const timestamp = Date.parse(value || "");
    if (!Number.isFinite(timestamp)) return "recently";
    const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
    const hours = Math.floor(minutes / 60);
    return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  }

  function presentSnapshotMode(event) {
    if (event?.detail?.source !== "deployed-snapshot") return;
    const banner = document.getElementById(SNAPSHOT_BANNER_ID);
    if (!banner) return;
    banner.dataset.mode = "snapshot";
    banner.textContent = `Snapshot mode · inventory refreshed ${ageLabel(event.detail.cachedAt)}. Search and apply links remain available.`;
    Object.assign(banner.style, {
      borderBottomColor: "#8bb7e8",
      background: "#eef6ff",
      color: "#15395b",
    });
  }

  async function fetchWithDeadline(input, init = {}) {
    const sourceSignal = init.signal || (input instanceof Request ? input.signal : null);
    if (sourceSignal?.aborted) throw sourceSignal.reason || new DOMException("Aborted", "AbortError");
    const controller = new AbortController();
    const onAbort = () => controller.abort(sourceSignal?.reason);
    sourceSignal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => controller.abort(new DOMException("Snapshot fetch timed out", "TimeoutError")), FETCH_TIMEOUT_MS);
    try {
      return await nativeFetch(input, { ...init, signal: controller.signal });
    } finally {
      clearTimeout(timer);
      sourceSignal?.removeEventListener("abort", onAbort);
    }
  }

  async function remoteSnapshot(init) {
    const version = Math.floor(Date.now() / 60000);
    const headers = new Headers(init?.headers || {});
    headers.set("Accept", "application/json");
    const response = await fetchWithDeadline(`${REMOTE_URL}?v=${version}`, {
      ...init,
      method: "GET",
      headers,
      cache: "no-store",
      credentials: "omit",
      mode: "cors",
    });
    if (!response.ok) throw new Error(`remote snapshot HTTP ${response.status}`);
    return response;
  }

  window.addEventListener("gaia:stale-data", presentSnapshotMode);

  window.fetch = async function snapshotTransport(input, init = {}) {
    if (!isSnapshotRequest(input, init)) return nativeFetch(input, init);
    try {
      return await remoteSnapshot(init);
    } catch (error) {
      const sourceSignal = init.signal || (input instanceof Request ? input.signal : null);
      if (sourceSignal?.aborted) throw error;
      return fetchWithDeadline(input, init);
    }
  };
})();