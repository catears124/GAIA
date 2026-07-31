"use strict";

(() => {
  const nativeFetch = window.fetch.bind(window);
  const CACHE_NAME = "gaia-api-v2";
  const MAX_EMERGENCY_AGE_MS = 30 * 24 * 60 * 60 * 1000;
  const API_PREFIX = "/api/";
  const READ_ENDPOINTS = new Set(["health", "stats", "facets", "families", "coverage", "universe"]);

  function urlFor(input) {
    try { return new URL(input instanceof Request ? input.url : input, location.href); }
    catch { return null; }
  }

  function eligible(input, init = {}) {
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const url = urlFor(input);
    if (method !== "GET" || !url || url.origin !== location.origin || !url.pathname.startsWith(API_PREFIX)) return false;
    return READ_ENDPOINTS.has(url.pathname.slice(API_PREFIX.length).split("/", 1)[0]);
  }

  function outageBanner(cachedAt) {
    let node = document.querySelector("#gaia-emergency-banner");
    if (!node) {
      node = document.createElement("div");
      node.id = "gaia-emergency-banner";
      node.setAttribute("role", "status");
      node.setAttribute("aria-live", "polite");
      node.className = "gaia-emergency-banner";
      const topbar = document.querySelector(".topbar");
      if (topbar?.parentNode) topbar.parentNode.insertBefore(node, topbar.nextSibling);
      else document.body.prepend(node);
    }
    const ageHours = Math.max(1, Math.floor((Date.now() - cachedAt) / 3600000));
    const age = ageHours < 48 ? `${ageHours}h old` : `${Math.floor(ageHours / 24)}d old`;
    node.textContent = `Live inventory is offline. Showing the last inventory saved on this device (${age}).`;
  }

  function clearBanner() {
    document.querySelector("#gaia-emergency-banner")?.remove();
  }

  async function emergencyCached(request) {
    if (!("caches" in window)) return null;
    try {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(request, { ignoreVary: true });
      if (!cached) return null;
      const raw = cached.headers.get("X-GAIA-Cached-At");
      const cachedAt = Date.parse(raw || "");
      if (!Number.isFinite(cachedAt) || Date.now() - cachedAt > MAX_EMERGENCY_AGE_MS) return null;
      const headers = new Headers(cached.headers);
      headers.set("X-GAIA-Stale", "1");
      headers.set("X-GAIA-Emergency-Cache", "1");
      headers.set("Cache-Control", "no-store");
      let body = await cached.text();
      if (urlFor(request)?.pathname === "/api/health") {
        try {
          const data = JSON.parse(body);
          data.ok = false;
          data.healthy = false;
          data.stale = true;
          data.running = false;
          data.inventory = { ...(data.inventory || {}), healthy: false, stale_snapshot: true };
          body = JSON.stringify(data);
        } catch { /* preserve cache body */ }
      }
      outageBanner(cachedAt);
      return new Response(body, { status: 200, statusText: "Emergency cache", headers });
    } catch { return null; }
  }

  window.fetch = async function emergencyFetch(input, init = {}) {
    if (!eligible(input, init)) return nativeFetch(input, init);
    const request = input instanceof Request ? input : new Request(input, init);
    try {
      const response = await nativeFetch(request.clone());
      if (response.ok) clearBanner();
      if (response.status < 500) return response;
      return await emergencyCached(request) || response;
    } catch (error) {
      return await emergencyCached(request) || Promise.reject(error);
    }
  };

  window.addEventListener("DOMContentLoaded", () => {
    document.body.addEventListener("click", event => {
      if (!event.target.closest("[data-retry]")) return;
      const empty = document.querySelector("#empty-state");
      if (empty) empty.innerHTML = '<strong>Inventory is temporarily offline.</strong><p>GAIA is retrying automatically. Filters and saved jobs remain available.</p>';
    });
  });
})();
