"use strict";

(() => {
  const nativeFetch = window.fetch.bind(window);
  const CACHE_NAME = "gaia-api-v2";
  const LOCAL_KEY = "gaia:emergency-api-v1";
  const MAX_EMERGENCY_AGE_MS = 30 * 24 * 60 * 60 * 1000;
  const REQUEST_DEADLINE_MS = 9000;
  const MAX_LOCAL_ENTRIES = 48;
  const MAX_LOCAL_BYTES = 3_500_000;
  const API_PREFIX = "/api/";
  const READ_ENDPOINTS = new Set(["health", "stats", "facets", "families", "coverage", "universe"]);

  function urlFor(input) {
    try { return new URL(input instanceof Request ? input.url : input, location.href); }
    catch { return null; }
  }

  function normalizedKey(input) {
    const url = urlFor(input);
    if (!url) return null;
    const pairs = [...url.searchParams.entries()]
      .filter(([, value]) => value !== "" && value !== "0" && value !== "false")
      .sort(([ak, av], [bk, bv]) => ak.localeCompare(bk) || av.localeCompare(bv));
    const query = new URLSearchParams(pairs).toString();
    return `${url.pathname}${query ? `?${query}` : ""}`;
  }

  function eligible(input, init = {}) {
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const url = urlFor(input);
    if (method !== "GET" || !url || url.origin !== location.origin || !url.pathname.startsWith(API_PREFIX)) return false;
    return READ_ENDPOINTS.has(url.pathname.slice(API_PREFIX.length).split("/", 1)[0]);
  }

  function ageLabel(cachedAt) {
    const ageHours = Math.max(1, Math.floor((Date.now() - cachedAt) / 3600000));
    return ageHours < 48 ? `${ageHours}h old` : `${Math.floor(ageHours / 24)}d old`;
  }

  function outageBanner(cachedAt, source = "device") {
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
    const kind = source === "local" ? "durable device backup" : "browser cache";
    node.textContent = `Live inventory is offline. Showing ${kind} (${ageLabel(cachedAt)}).`;
    document.documentElement.dataset.gaiaOffline = "true";
  }

  function clearBanner() {
    document.querySelector("#gaia-emergency-banner")?.remove();
    delete document.documentElement.dataset.gaiaOffline;
  }

  function truthfulBody(request, body) {
    if (urlFor(request)?.pathname !== "/api/health") return body;
    try {
      const data = JSON.parse(body);
      data.ok = false;
      data.healthy = false;
      data.stale = true;
      data.running = false;
      data.inventory = { ...(data.inventory || {}), healthy: false, stale_snapshot: true };
      return JSON.stringify(data);
    } catch { return body; }
  }

  function responseFromBody(request, body, cachedAt, source) {
    const headers = new Headers({
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      "X-GAIA-Stale": "1",
      "X-GAIA-Emergency-Cache": "1",
      "X-GAIA-Cached-At": new Date(cachedAt).toISOString(),
    });
    if (source === "local") headers.set("X-GAIA-Durable-Backup", "1");
    outageBanner(cachedAt, source);
    window.dispatchEvent(new CustomEvent("gaia:stale-data", {
      detail: { cachedAt: new Date(cachedAt).toISOString(), source: `emergency-${source}` },
    }));
    return new Response(truthfulBody(request, body), { status: 200, statusText: "Emergency cache", headers });
  }

  function readLocal() {
    try {
      const value = JSON.parse(localStorage.getItem(LOCAL_KEY) || "{}");
      return value && typeof value === "object" ? value : {};
    } catch { return {}; }
  }

  function writeLocal(entries) {
    try {
      let rows = Object.entries(entries)
        .filter(([, entry]) => entry && Number.isFinite(entry.cachedAt) && typeof entry.body === "string")
        .filter(([, entry]) => Date.now() - entry.cachedAt <= MAX_EMERGENCY_AGE_MS)
        .sort(([, left], [, right]) => right.cachedAt - left.cachedAt)
        .slice(0, MAX_LOCAL_ENTRIES);
      while (rows.length && JSON.stringify(Object.fromEntries(rows)).length > MAX_LOCAL_BYTES) rows.pop();
      localStorage.setItem(LOCAL_KEY, JSON.stringify(Object.fromEntries(rows)));
    } catch { /* Storage pressure must never break the live product. */ }
  }

  async function rememberDurable(request, response) {
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok || !contentType.includes("application/json")) return;
    const key = normalizedKey(request);
    if (!key) return;
    try {
      const body = await response.clone().text();
      JSON.parse(body);
      const entries = readLocal();
      entries[key] = { cachedAt: Date.now(), body };
      writeLocal(entries);
    } catch { /* Malformed or oversized responses are not durable fallbacks. */ }
  }

  function durableCached(request) {
    const key = normalizedKey(request);
    if (!key) return null;
    const entry = readLocal()[key];
    if (!entry || !Number.isFinite(entry.cachedAt) || typeof entry.body !== "string") return null;
    if (Date.now() - entry.cachedAt > MAX_EMERGENCY_AGE_MS) return null;
    return responseFromBody(request, entry.body, entry.cachedAt, "local");
  }

  async function cacheStorageCached(request) {
    if (!("caches" in window)) return null;
    try {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(request, { ignoreVary: true });
      if (!cached) return null;
      const raw = cached.headers.get("X-GAIA-Cached-At");
      const cachedAt = Date.parse(raw || "");
      if (!Number.isFinite(cachedAt) || Date.now() - cachedAt > MAX_EMERGENCY_AGE_MS) return null;
      return responseFromBody(request, await cached.text(), cachedAt, "cache");
    } catch { return null; }
  }

  async function emergencyCached(request) {
    return durableCached(request) || await cacheStorageCached(request);
  }

  async function liveHealthy(request, response) {
    if (!response.ok) return false;
    if (urlFor(request)?.pathname !== "/api/health") return true;
    try {
      const data = await response.clone().json();
      return data.ok === true && data.healthy !== false && data.inventory?.healthy !== false;
    } catch { return false; }
  }

  function withDeadline(promise) {
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new DOMException("GAIA API deadline exceeded", "TimeoutError")), REQUEST_DEADLINE_MS);
    });
    return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
  }

  window.fetch = async function emergencyFetch(input, init = {}) {
    if (!eligible(input, init)) return nativeFetch(input, init);
    const request = input instanceof Request ? input : new Request(input, init);
    try {
      const response = await withDeadline(nativeFetch(request.clone()));
      if (response.ok) {
        void rememberDurable(request, response);
        if (await liveHealthy(request, response)) clearBanner();
      }
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
