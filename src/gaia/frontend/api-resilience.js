"use strict";

(() => {
  const CACHE_NAME = "gaia-api-v1";
  const API_PREFIX = "/api/";
  const CACHEABLE = new Set(["health", "stats", "facets", "families", "coverage", "universe"]);
  const nativeFetch = window.fetch.bind(window);
  let staleBanner;

  function isCacheable(input, init = {}) {
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method !== "GET") return false;
    try {
      const url = new URL(input instanceof Request ? input.url : input, location.href);
      if (url.origin !== location.origin || !url.pathname.startsWith(API_PREFIX)) return false;
      const resource = url.pathname.slice(API_PREFIX.length).split("/", 1)[0];
      return CACHEABLE.has(resource);
    } catch {
      return false;
    }
  }

  function showStaleBanner() {
    if (staleBanner) return;
    staleBanner = document.createElement("div");
    staleBanner.id = "gaia-stale-data-banner";
    staleBanner.setAttribute("role", "status");
    staleBanner.textContent = "Live database unavailable. Showing the most recent cached inventory.";
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

  async function cachedResponse(request) {
    if (!("caches" in window)) return null;
    try {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(request);
      if (!cached) return null;
      const headers = new Headers(cached.headers);
      headers.set("X-GAIA-Stale", "1");
      headers.set("X-GAIA-Cached-At", headers.get("X-GAIA-Cached-At") || "unknown");
      const body = await cached.blob();
      showStaleBanner();
      window.dispatchEvent(new CustomEvent("gaia:stale-data"));
      return new Response(body, { status: 200, statusText: "Cached", headers });
    } catch {
      return null;
    }
  }

  async function remember(request, response) {
    if (!("caches" in window) || !response.ok) return;
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) return;
    try {
      const cache = await caches.open(CACHE_NAME);
      const headers = new Headers(response.headers);
      headers.set("X-GAIA-Cached-At", new Date().toISOString());
      const body = await response.clone().blob();
      await cache.put(request, new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers,
      }));
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
