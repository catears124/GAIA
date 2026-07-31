"use strict";

(() => {
  const CACHE_NAME = "gaia-api-v2";
  const API_PREFIX = "/api/";
  const CACHEABLE = new Set(["health", "stats", "facets", "families", "coverage", "universe"]);
  const MAX_STALE_MS = 24 * 60 * 60 * 1000;
  const MAX_CACHE_ENTRIES = 120;
  const nativeFetch = window.fetch.bind(window);
  let staleBanner;

  function requestUrl(input) {
    try {
      return new URL(input instanceof Request ? input.url : input, location.href);
    } catch {
      return null;
    }
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

  function showStaleBanner(cachedAt) {
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
    staleBanner.textContent = `Live database unavailable. Showing cached inventory${age === null ? "" : ` (${ageLabel(age)})`}.`;
  }

  function clearStaleBanner() {
    if (!staleBanner) return;
    staleBanner.remove();
    staleBanner = undefined;
    window.dispatchEvent(new CustomEvent("gaia:live-data"));
  }

  async function truthfulCachedBody(request, cached) {
    const url = requestUrl(request);
    const body = await cached.blob();
    if (url?.pathname !== "/api/health") return body;
    try {
      const payload = JSON.parse(await body.text());
      payload.ok = false;
      payload.stale = true;
      payload.running = false;
      payload.inventory = { ...(payload.inventory || {}), healthy: false, stale_snapshot: true };
      return new Blob([JSON.stringify(payload)], { type: "application/json" });
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
      const body = await truthfulCachedBody(request, cached);
      showStaleBanner(cachedAt);
      window.dispatchEvent(new CustomEvent("gaia:stale-data", { detail: { cachedAt: cachedAtRaw } }));
      return new Response(body, { status: 200, statusText: "Cached", headers });
    } catch {
      return null;
    }
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
        const cached = await cachedResponse(request);
        if (cached) return cached;
      }
      return response;
    } catch (error) {
      const cached = await cachedResponse(request);
      if (cached) return cached;
      throw error;
    }
  };
})();
