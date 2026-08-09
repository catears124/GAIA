"use strict";

(() => {
  const previousFetch = window.fetch.bind(window);
  const SNAPSHOT_PATH = "/assets/last-known-inventory.json";
  const TECH_CATEGORIES = new Set([
    "software", "ml-ai", "quant", "security", "data", "product", "other-technical",
  ]);
  const TARGET_MATCHES = new Set(["exact", "year_confirmed", "source_confirmed"]);
  const SNAPSHOT_REFRESH_MS = 60 * 1000;
  let snapshotPromise;
  let snapshotFetchedAt = 0;
  let staleMode = null;
  let staleCachedAt = null;
  let healthObserver;

  function requestUrl(input) {
    try { return new URL(input instanceof Request ? input.url : input, location.href); }
    catch { return null; }
  }

  function parseTime(value) {
    const parsed = Date.parse(value || "");
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function itemActivity(item) {
    const posted = parseTime(item.latest_posted_at);
    return posted || parseTime(item.first_detected_at);
  }

  function verifiedActivity(item) {
    return parseTime(item.last_verified_at);
  }

  function isVerified(item) {
    return Boolean(item.verified) || Number(item.direct_openings || 0) > 0;
  }

  function isDated(item) {
    return parseTime(item.latest_posted_at) > 0;
  }

  function matchesTarget(item, target) {
    if (!target) return true;
    const match = String(item.target_match || "");
    if (target === "default") return TARGET_MATCHES.has(match);
    return match === target;
  }

  function compareText(left, right) {
    return String(left || "").localeCompare(String(right || ""), undefined, { sensitivity: "base" });
  }

  function compareNewest(left, right) {
    return Number(isVerified(right)) - Number(isVerified(left)) ||
      Number(isDated(right)) - Number(isDated(left)) ||
      itemActivity(right) - itemActivity(left) ||
      verifiedActivity(right) - verifiedActivity(left) ||
      compareText(left.family_key, right.family_key);
  }

  function compareVerified(left, right) {
    return Number(isVerified(right)) - Number(isVerified(left)) ||
      verifiedActivity(right) - verifiedActivity(left) ||
      Number(isDated(right)) - Number(isDated(left)) ||
      itemActivity(right) - itemActivity(left) ||
      compareText(left.family_key, right.family_key);
  }

  function compareCompany(left, right) {
    return compareText(left.company, right.company) ||
      compareText(left.title, right.title) ||
      compareText(left.family_key, right.family_key);
  }

  function filterIndex(index, url) {
    const tokens = (url.searchParams.get("q") || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
    const category = url.searchParams.get("category") || "";
    const target = url.searchParams.get("target") || "";
    const trust = url.searchParams.get("trust") || "all";
    const track = url.searchParams.get("track") || "tech";
    const company = (url.searchParams.get("company") || "").trim().toLowerCase();
    const locationQuery = (url.searchParams.get("location") || "").trim().toLowerCase();
    const remote = url.searchParams.get("remote") === "true";
    const postedWithin = Math.max(0, Number(url.searchParams.get("posted_within") || 0));
    const cutoff = postedWithin ? Date.now() - postedWithin * 86400000 : 0;

    return index.filter(item => {
      if (!item || typeof item !== "object") return false;
      const locations = Array.isArray(item.locations) ? item.locations : [];
      const locationText = locations.join(" ").toLowerCase();
      const haystack = `${item.title || ""} ${item.company || ""} ${locationText}`.toLowerCase();
      const verified = isVerified(item);
      if (track === "tech" && !TECH_CATEGORIES.has(String(item.category || ""))) return false;
      if (tokens.some(token => !haystack.includes(token))) return false;
      if (category && item.category !== category) return false;
      if (!matchesTarget(item, target)) return false;
      if (trust === "verified" && !verified) return false;
      if (trust === "leads" && verified) return false;
      if (company && String(item.company || "").toLowerCase() !== company) return false;
      if (locationQuery && !locationText.includes(locationQuery)) return false;
      if (remote && !(item.remote || locationText.includes("remote"))) return false;
      if (cutoff && itemActivity(item) < cutoff) return false;
      return true;
    });
  }

  function familyPayload(snapshot, url) {
    if (!Array.isArray(snapshot?.family_index)) return null;
    const items = filterIndex(snapshot.family_index, url);
    const sort = url.searchParams.get("sort") || "newest";
    items.sort(sort === "company" ? compareCompany : sort === "verified" ? compareVerified : compareNewest);
    const page = Math.max(1, Number(url.searchParams.get("page") || 1));
    const pageSize = Math.min(100, Math.max(12, Number(url.searchParams.get("page_size") || 48)));
    const start = (page - 1) * pageSize;
    return {
      items: items.slice(start, start + pageSize),
      total: items.length,
      page,
      page_size: pageSize,
      stale: true,
      offline: true,
      snapshot_generated_at: snapshot.generated_at || null,
      source_activity_at: snapshot.source_activity_at || null,
    };
  }

  function statsPayload(snapshot) {
    if (!Array.isArray(snapshot?.family_index)) return null;
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    const items = snapshot.family_index.filter(item =>
      item && typeof item === "object" && isVerified(item) && TECH_CATEGORIES.has(String(item.category || ""))
    );
    const companies = new Set(items.map(item => String(item.company || "").trim().toLowerCase()).filter(Boolean));
    const active = items.reduce((sum, item) => sum + Number(item.direct_openings || 0), 0);
    const newFamilies = items.filter(item => parseTime(item.first_detected_at) >= cutoff).length;
    return {
      role_families: items.length,
      active_listings: active,
      companies: companies.size,
      new_today: newFamilies,
      new_24h: newFamilies,
      new_families_24h: newFamilies,
      verified_listings: active,
      verified_families: items.length,
      activity_units: { new_today: "role_family", url_movement: "canonical_apply_url" },
      stale: true,
      snapshot_generated_at: snapshot.generated_at || null,
      source_activity_at: snapshot.source_activity_at || null,
    };
  }

  async function loadSnapshot() {
    const now = Date.now();
    if (!snapshotPromise || now - snapshotFetchedAt >= SNAPSHOT_REFRESH_MS) {
      snapshotFetchedAt = now;
      const minute = Math.floor(now / 60000);
      snapshotPromise = previousFetch(`${SNAPSHOT_PATH}?v=${minute}`, {
        headers: { Accept: "application/json" }, cache: "no-store",
      }).then(response => {
        if (!response.ok) throw new Error(`snapshot HTTP ${response.status}`);
        return response.json();
      }).catch(() => null);
    }
    return snapshotPromise;
  }

  function jsonResponse(payload, original) {
    const headers = new Headers(original.headers);
    headers.set("Content-Type", "application/json");
    headers.set("Cache-Control", "no-store");
    headers.set("X-GAIA-Feed-Contract", "verified-first-v1");
    headers.set("X-GAIA-Stale", "1");
    return new Response(JSON.stringify(payload), { status: 200, statusText: "Snapshot", headers });
  }

  window.fetch = async function feedContractFetch(input, init = {}) {
    const url = requestUrl(input);
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const response = await previousFetch(input, init);
    if (method !== "GET" || !url || url.origin !== location.origin) return response;
    if (!["/api/families", "/api/stats"].includes(url.pathname)) return response;

    const stale = response.headers.get("X-GAIA-Stale") === "1" ||
      response.headers.get("X-GAIA-Snapshot") === "1";
    if (!stale) return response;

    const snapshot = await loadSnapshot();
    if (!snapshot) return response;
    const payload = url.pathname === "/api/families"
      ? familyPayload(snapshot, url)
      : statsPayload(snapshot);
    return payload ? jsonResponse(payload, response) : response;
  };

  function ageLabel(value) {
    const timestamp = Date.parse(value || "");
    if (!Number.isFinite(timestamp)) return "snapshot";
    const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes}m old`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h old`;
  }

  function enforceHealthTruth() {
    if (!staleMode) return;
    const node = document.querySelector("#freshness");
    if (!node) return;
    const label = staleMode === "browser-cache" ? "Cached inventory" : "Snapshot";
    const text = `${label} · ${ageLabel(staleCachedAt)}`;
    if (node.lastElementChild?.textContent !== text) node.lastElementChild.textContent = text;
    if (!node.classList.contains("stale") || node.classList.contains("fresh") || node.classList.contains("failed")) {
      node.classList.remove("fresh", "failed");
      node.classList.add("stale");
    }
  }

  function ensureHealthObserver() {
    const node = document.querySelector("#freshness");
    if (!node || healthObserver) return;
    healthObserver = new MutationObserver(enforceHealthTruth);
    healthObserver.observe(node, { childList: true, subtree: true, attributes: true, characterData: true });
  }

  window.addEventListener("gaia:stale-data", event => {
    staleMode = event.detail?.source || "snapshot";
    staleCachedAt = event.detail?.cachedAt || null;
    ensureHealthObserver();
    queueMicrotask(enforceHealthTruth);
  });

  window.addEventListener("gaia:live-data", () => {
    staleMode = null;
    staleCachedAt = null;
  });

  // app-v2's legacy reset default was "All". The product default is now verified;
  // preserve that invariant after a reset without changing explicit user selections.
  document.addEventListener("click", event => {
    if (!event.target.closest("#clear-filters")) return;
    setTimeout(() => {
      const trust = document.querySelector("#trust");
      if (trust && trust.value === "all") {
        trust.value = "verified";
        trust.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }, 0);
  }, true);

  document.addEventListener("DOMContentLoaded", ensureHealthObserver, { once: true });
})();
