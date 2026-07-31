"use strict";

(() => {
  const CACHE_NAME = "gaia-api-v2";
  const API_PREFIX = "/api/";
  const CACHEABLE = new Set(["health", "stats", "facets", "families", "coverage", "universe"]);
  const MAX_STALE_MS = 24 * 60 * 60 * 1000;
  const MAX_CACHE_ENTRIES = 120;
  const TARGET_MATCHES = new Set(["exact", "year_confirmed", "source_confirmed"]);
  const nativeFetch = window.fetch.bind(window);
  let staleBanner;
  let staticSnapshotPromise;

  function requestUrl(input) {
    try { return new URL(input instanceof Request ? input.url : input, location.href); }
    catch { return null; }
  }

  function normalizedKey(input) {
    const url = requestUrl(input);
    if (!url) return null;
    const params = [...url.searchParams.entries()].filter(([, value]) => value !== "" && value !== "0" && value !== "false");
    params.sort(([ak, av], [bk, bv]) => ak.localeCompare(bk) || av.localeCompare(bv));
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
        position: "sticky", top: "58px", zIndex: "29", width: "100%", padding: ".55rem 1rem",
        borderBottom: "1px solid #a46c17", color: "#5b3905", background: "#fff0cf",
        textAlign: "center", fontSize: ".75rem", fontWeight: "700",
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
    } catch { return body; }
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
      const body = await truthfulBody(request, await cached.blob());
      showStaleBanner(cachedAt, "cached");
      window.dispatchEvent(new CustomEvent("gaia:stale-data", { detail: { cachedAt: cachedAtRaw, source: "browser-cache" } }));
      return new Response(body, { status: 200, statusText: "Cached", headers });
    } catch { return null; }
  }

  async function loadStaticSnapshot() {
    if (!staticSnapshotPromise) {
      const minute = Math.floor(Date.now() / 60000);
      staticSnapshotPromise = nativeFetch(`/assets/last-known-inventory.json?v=${minute}`, {
        headers: { Accept: "application/json" }, cache: "no-store",
      }).then(response => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      }).catch(() => null);
    }
    return staticSnapshotPromise;
  }

  function itemActivity(item) {
    const posted = Date.parse(item.latest_posted_at || "");
    const found = Date.parse(item.first_detected_at || "");
    return Math.max(Number.isFinite(posted) ? posted : 0, Number.isFinite(found) ? found : 0);
  }

  function verifiedActivity(item) {
    const value = Date.parse(item.last_verified_at || "");
    return Number.isFinite(value) ? value : 0;
  }

  function matchesTarget(item, target) {
    if (!target) return true;
    const match = String(item.target_match || "");
    if (target === "default") return TARGET_MATCHES.has(match);
    return match === target;
  }

  function filterFamilyIndex(index, url) {
    const queryTokens = (url.searchParams.get("q") || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
    const category = url.searchParams.get("category") || "";
    const target = url.searchParams.get("target") || "";
    const trust = url.searchParams.get("trust") || "all";
    const company = url.searchParams.get("company") || "";
    const locationQuery = (url.searchParams.get("location") || "").trim().toLowerCase();
    const remote = url.searchParams.get("remote") === "true";
    const postedWithin = Math.max(0, Number(url.searchParams.get("posted_within") || 0));
    const cutoff = postedWithin ? Date.now() - postedWithin * 86400000 : 0;

    return index.filter(item => {
      const locations = Array.isArray(item.locations) ? item.locations : [];
      const locationText = locations.join(" ").toLowerCase();
      const haystack = `${item.title || ""} ${item.company || ""} ${locationText}`.toLowerCase();
      if (queryTokens.some(token => !haystack.includes(token))) return false;
      if (category && item.category !== category) return false;
      if (!matchesTarget(item, target)) return false;
      if (trust === "verified" && !item.verified) return false;
      if (trust === "leads" && item.verified) return false;
      if (company && String(item.company || "").toLowerCase() !== company.toLowerCase()) return false;
      if (locationQuery && !locationText.includes(locationQuery)) return false;
      if (remote && !(item.remote || locationText.includes("remote"))) return false;
      if (cutoff && itemActivity(item) < cutoff) return false;
      return true;
    });
  }

  function offlineFamilies(snapshot, url) {
    if (!Array.isArray(snapshot.family_index)) return null;
    const sort = url.searchParams.get("sort") || "newest";
    const page = Math.max(1, Number(url.searchParams.get("page") || 1));
    const pageSize = Math.min(100, Math.max(12, Number(url.searchParams.get("page_size") || 48)));
    const items = filterFamilyIndex(snapshot.family_index, url);
    items.sort((left, right) => {
      if (sort === "company") {
        return String(left.company || "").localeCompare(String(right.company || "")) ||
          String(left.title || "").localeCompare(String(right.title || "")) ||
          String(left.family_key || "").localeCompare(String(right.family_key || ""));
      }
      if (sort === "verified") {
        return verifiedActivity(right) - verifiedActivity(left) || itemActivity(right) - itemActivity(left);
      }
      return itemActivity(right) - itemActivity(left) || verifiedActivity(right) - verifiedActivity(left);
    });
    const start = (page - 1) * pageSize;
    return { items: items.slice(start, start + pageSize), total: items.length, page, page_size: pageSize, offline: true };
  }

  function offlineFacets(snapshot, url) {
    if (!Array.isArray(snapshot.family_index)) return null;
    const synthetic = new URL(url.href);
    synthetic.searchParams.delete("company");
    synthetic.searchParams.delete("q");
    synthetic.searchParams.delete("location");
    synthetic.searchParams.delete("page");
    synthetic.searchParams.delete("page_size");
    const companyCounts = new Map();
    const categoryCounts = new Map();
    let remoteCount = 0;
    for (const item of filterFamilyIndex(snapshot.family_index, synthetic)) {
      const company = String(item.company || "").trim();
      const category = String(item.category || "").trim();
      const locations = Array.isArray(item.locations) ? item.locations.join(" ").toLowerCase() : "";
      if (company) companyCounts.set(company, (companyCounts.get(company) || 0) + 1);
      if (category) categoryCounts.set(category, (categoryCounts.get(category) || 0) + 1);
      if (item.remote || locations.includes("remote")) remoteCount += 1;
    }
    const ranked = counts => [...counts.entries()]
      .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
      .map(([value, count]) => ({ value, count }));
    return {
      companies: ranked(companyCounts),
      categories: ranked(categoryCounts),
      remote_count: remoteCount,
      offline: true,
    };
  }

  function derivedSnapshotPayload(snapshot, request) {
    const url = requestUrl(request);
    if (!url) return null;
    if (url.pathname === "/api/families") return offlineFamilies(snapshot, url);
    if (url.pathname === "/api/facets") return offlineFacets(snapshot, url);
    return null;
  }

  async function staticSnapshotResponse(request) {
    try {
      const snapshot = await loadStaticSnapshot();
      const generatedAtRaw = snapshot?.generated_at;
      const generatedAt = generatedAtRaw ? Date.parse(generatedAtRaw) : Number.NaN;
      const maxAge = Math.min(MAX_STALE_MS, Number(snapshot?.max_stale_seconds || 86400) * 1000);
      if (!Number.isFinite(generatedAt) || Date.now() - generatedAt > maxAge) return null;
      const key = normalizedKey(request);
      const exact = key ? snapshot.responses?.[key] : null;
      const payload = exact || derivedSnapshotPayload(snapshot, request);
      if (!payload) return null;
      const body = await truthfulBody(request, JSON.stringify(payload));
      const headers = new Headers({
        "Content-Type": "application/json", "Cache-Control": "no-store", "X-GAIA-Stale": "1",
        "X-GAIA-Snapshot": "1", "X-GAIA-Cached-At": generatedAtRaw,
      });
      if (!exact) headers.set("X-GAIA-Offline-Search", "1");
      showStaleBanner(generatedAt, "snapshot");
      window.dispatchEvent(new CustomEvent("gaia:stale-data", { detail: { cachedAt: generatedAtRaw, source: "deployed-snapshot" } }));
      return new Response(body, { status: 200, statusText: "Snapshot", headers });
    } catch { return null; }
  }

  async function fallbackResponse(request) {
    return await cachedResponse(request) || await staticSnapshotResponse(request);
  }

  async function prune(cache) {
    const entries = [];
    for (const request of await cache.keys()) {
      const response = await cache.match(request);
      const cachedAt = Date.parse(response?.headers.get("X-GAIA-Cached-At") || "");
      if (!Number.isFinite(cachedAt) || Date.now() - cachedAt > MAX_STALE_MS) await cache.delete(request);
      else entries.push([request, cachedAt]);
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
      await cache.put(request, new Response(await response.clone().blob(), {
        status: response.status, statusText: response.statusText, headers,
      }));
      void prune(cache);
    } catch { /* Cache failure must never block the live product. */ }
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
