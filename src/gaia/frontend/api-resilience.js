"use strict";

(() => {
  const CACHE_NAME = "gaia-api-v2";
  const API_PREFIX = "/api/";
  const CACHEABLE = new Set(["health", "stats", "facets", "families", "coverage", "universe"]);
  const MAX_STALE_MS = 24 * 60 * 60 * 1000;
  const MAX_CACHE_ENTRIES = 120;
  const nativeFetch = window.fetch.bind(window);
  let staleBanner;
  let staticSnapshotPromise;

  function requestUrl(input) {
    try {
      return new URL(input instanceof Request ? input.url : input, location.href);
    } catch {
      return null;
    }
  }

  function normalizedKey(input) {
    const url = requestUrl(input);
    if (!url) return null;
    const params = [...url.searchParams.entries()].filter(([, value]) => value !== "" && value !== "0" && value !== "false");
    params.sort(([leftKey, leftValue], [rightKey, rightValue]) => leftKey.localeCompare(rightKey) || leftValue.localeCompare(rightValue));
    const query = new URLSearchParams(params).toString();
    return `${url.pathname}${query ? `?${query}` : ""}`;
  }

  function isCacheable(input, init = {}) {
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method !== "GET") return false;
    const url = requestUrl(input);
    if (!url || url.origin !== location.origin || !url.pathname.startsWith(API_PREFIX)) return false;
    const resource = url.pathname.slice(API_PREFIX.length).split("/", 1)[0];
    return CACHEABLE.has(resource);
  }

  function ageLabel(milliseconds) {
    const minutes = Math.max(0, Math.floor(milliseconds / 60000));
    if (minutes < 1) return "less than a minute old";
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} old`;
    const hours = Math.floor(minutes / 60);
    return `${hours} hour${hours === 1 ? "" : "s"} old`;
  }

  function showStaleBanner(cachedAt, source = "cached") {
    const age = Number.isFinite(cachedAt) ? Date.now() - cachedAt : null;
    if (!staleBanner) {
      staleBanner = document.createElement("div");
      staleBanner.id = "gaia-stale-data-banner";
      staleBanner.setAttribute("role", "status");
      staleBanner.setAttribute("aria-live", "polite");
      Object.assign(staleBanner.style, {
        position: "sticky",
        top: "58px",
        zIndex: "29",
        width: "100%",
        padding: ".55rem 1rem",
        borderBottom: "1px solid #a46c17",
        color: "#5b3905",
        background: "#fff0cf",
        textAlign: "center",
        fontSize: ".75rem",
        fontWeight: "700",
      });
      const topbar = document.querySelector(".topbar");
      if (topbar?.parentNode) topbar.parentNode.insertBefore(staleBanner, topbar.nextSibling);
      else document.body.prepend(staleBanner);
    }
    const kind = source === "snapshot" ? "last deployed inventory" : "cached inventory";
    staleBanner.textContent = `Live database unavailable. Showing ${kind}${age === null ? "" : ` (${ageLabel(age)})`}.`;
  }

  function clearStaleBanner() {
    if (!staleBanner) return;
    staleBanner.remove();
    staleBanner = undefined;
    window.dispatchEvent(new CustomEvent("gaia:live-data"));
  }

  async function truthfulBody(request, body) {
    const url = requestUrl(request);
    if (url?.pathname !== "/api/health") return body;
    try {
      const payload = JSON.parse(typeof body === "string" ? body : await body.text());
      payload.ok = false;
      payload.stale = true;
      payload.running = false;
      payload.inventory = { ...(payload.inventory || {}), healthy: false, stale_snapshot: true };
      return JSON.stringify(payload);
    } catch {
      return body;
    }
  }

  async function cachedResponse(request) {
    if (!("caches" in window)) return null;
    try {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(request);
      if (!cached) return null;
      const cachedAtRaw = cached.headers.get("X-GAIA-Cached-At");
      const cachedAt = cachedAtRaw ? Date.parse(cachedAtRaw) : Number.NaN;
      if (!Number.isFinite(cachedAt) || Date.now() - cachedAt > MAX_STALE_MS) {
        await cache.delete(request);
        return null;
      }
      const headers = new Headers(cached.headers);
      headers.set("X-GAIA-Stale", "1");
      headers.set("Cache-Control", "no-store");
      const original = await cached.blob();
      const body = await truthfulBody(request, original);
      showStaleBanner(cachedAt, "cached");
      window.dispatchEvent(new CustomEvent("gaia:stale-data", { detail: { cachedAt: cachedAtRaw, source: "browser-cache" } }));
      return new Response(body, { status: 200, statusText: "Cached", headers });
    } catch {
      return null;
    }
  }

  async function loadStaticSnapshot() {
    if (!staticSnapshotPromise) {
      const minute = Math.floor(Date.now() / 60000);
      staticSnapshotPromise = nativeFetch(`/assets/last-known-inventory.json?v=${minute}`, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      }).then(response => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      }).catch(() => null);
    }
    return staticSnapshotPromise;
  }

  async function staticSnapshotResponse(request) {
    try {
      const snapshot = await loadStaticSnapshot();
      const generatedAtRaw = snapshot?.generated_at;
      const generatedAt = generatedAtRaw ? Date.parse(generatedAtRaw) : Number.NaN;
      const maxAge = Math.min(MAX_STALE_MS, Number(snapshot?.max_stale_seconds || 86400) * 1000);
      if (!Number.isFinite(generatedAt) || Date.now() - generatedAt > maxAge) return null;
      const key = normalizedKey(request);
      const payload = key ? snapshot.responses?.[key] : null;
      if (!payload) return null;
      const body = await truthfulBody(request, JSON.stringify(payload));
      const headers = new Headers({
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "X-GAIA-Stale": "1",
        "X-GAIA-Snapshot": "1",
        "X-GAIA-Cached-At": generatedAtRaw,
      });
      showStaleBanner(generatedAt, "snapshot");
      window.dispatchEvent(new CustomEvent("gaia:stale-data", { detail: { cachedAt: generatedAtRaw, source: "deployed-snapshot" } }));
      return new Response(body, { status: 200, statusText: "Snapshot", headers });
    } catch {
      return null;
    }
  }

  async function fallbackResponse(request) {
    return await cachedResponse(request) || await staticSnapshotResponse(request);
  }

  async function prune(cache) {
    const entries = [];
    for (const request of await cache.keys()) {
      const response = await cache.match(request);
      const cachedAt = Date.parse(response?.headers.get("X-GAIA-Cached-At") || "");
      if (!Number.isFinite(cachedAt) || Date.now() - cachedAt > MAX_STALE_MS) {
        await cache.delete(request);
      } else {
        entries.push([request, cachedAt]);
      }
    }
    entries.sort((left, right) => right[1] - left[1]);
    await Promise.all(entries.slice(MAX_CACHE_ENTRIES).map(([request]) => cache.delete(request)));
  }

  async function remember(request, response) {
    if (!("caches" in window) || !response.ok) return;
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) return;
    try {
      const cache = await caches.open(CACHE_NAME);
      const headers = new Headers(response.headers);
      headers.set("X-GAIA-Cached-At", new Date().toISOString());
      headers.set("Cache-Control", "no-store");
      const body = await response.clone().blob();
      await cache.put(request, new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers,
      }));
      void prune(cache);
    } catch {
      // Cache failure must never block the live product.
    }
  }

  window.fetch = async function resilientFetch(input, init = {}) {
    if (!isCacheable(input, init)) return nativeFetch(input, init);
    const request = input instanceof Request ? input : new Request(input, init);
    try {
      const response = await nativeFetch(request.clone());
      if (response.ok) {
        clearStaleBanner();
        void remember(request, response);
        return response;
      }
      if (response.status >= 500) {
        const fallback = await fallbackResponse(request);
        if (fallback) return fallback;
      }
      return response;
    } catch (error) {
      const fallback = await fallbackResponse(request);
      if (fallback) return fallback;
      throw error;
    }
  };
})();
