"use strict";

(() => {
  const upstreamFetch = window.fetch.bind(window);
  let snapshotPromise;

  function familyRequest(input, init = {}) {
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method !== "GET") return null;
    try {
      const url = new URL(input instanceof Request ? input.url : input, location.href);
      if (url.origin !== location.origin) return null;
      const match = url.pathname.match(/^\/api\/families\/([^/]+)$/);
      return match ? { url, key: decodeURIComponent(match[1]) } : null;
    } catch {
      return null;
    }
  }

  function loadSnapshot() {
    if (!snapshotPromise) {
      const minute = Math.floor(Date.now() / 60000);
      snapshotPromise = upstreamFetch(`/assets/last-known-inventory.json?v=${minute}`, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      }).then(response => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      }).catch(() => null);
    }
    return snapshotPromise;
  }

  function isLead(opening) {
    return ["registry", "external-index", "verification-lead"].includes(String(opening?.source_mode || ""));
  }

  function visibleFamily(item, trust) {
    const copy = structuredClone(item);
    const openings = Array.isArray(copy.openings) ? copy.openings : [];
    copy.openings = openings.filter(opening => {
      if (trust === "verified") return String(opening?.source_mode || "") === "direct";
      if (trust === "leads") return isLead(opening);
      return true;
    });
    if (trust === "verified" || trust === "leads") copy.opening_count = copy.openings.length;
    const locations = copy.openings.flatMap(opening => Array.isArray(opening.location) ? opening.location : []);
    if (locations.length) copy.locations = [...new Set(locations)];
    copy.offline = true;
    return copy;
  }

  async function snapshotFamily(request) {
    const snapshot = await loadSnapshot();
    const generatedAt = Date.parse(snapshot?.generated_at || "");
    const maxAge = Math.min(86400000, Number(snapshot?.max_stale_seconds || 86400) * 1000);
    if (!Number.isFinite(generatedAt) || Date.now() - generatedAt > maxAge) return null;
    const index = snapshot?.family_index;
    if (!Array.isArray(index)) return null;
    const item = index.find(candidate => candidate?.family_key === request.key);
    if (!item) return null;
    const trust = request.url.searchParams.get("trust") || "all";
    const payload = visibleFamily(item, trust);
    return new Response(JSON.stringify(payload), {
      status: 200,
      statusText: "Snapshot",
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "X-GAIA-Stale": "1",
        "X-GAIA-Snapshot": "1",
        "X-GAIA-Offline-Detail": "1",
        "X-GAIA-Cached-At": snapshot.generated_at,
      },
    });
  }

  window.fetch = async function offlineDetailFetch(input, init = {}) {
    const request = familyRequest(input, init);
    if (!request) return upstreamFetch(input, init);
    try {
      const response = await upstreamFetch(input, init);
      if (response.ok || response.status < 500) return response;
      return await snapshotFamily(request) || response;
    } catch (error) {
      const fallback = await snapshotFamily(request);
      if (fallback) return fallback;
      throw error;
    }
  };
})();
