"use strict";

(() => {
  const $ = selector => document.querySelector(selector);
  const formatNumber = value => new Intl.NumberFormat().format(Number(value || 0));
  const CACHE_KEY = "gaia:live-summary";

  const style = document.createElement("style");
  style.textContent = `
    .quick-actions{padding:.75rem .8rem .85rem;display:grid!important;grid-template-columns:repeat(3,minmax(0,1fr));gap:.5rem!important;margin:0!important;border-top:1px solid var(--line)}
    .quick-actions>span{grid-column:1/-1;margin:0!important;font-size:.62rem!important}
    .quick-actions button{width:100%;min-width:0;min-height:38px;padding:.45rem .55rem!important;font-size:.72rem!important;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .quick-actions .utility{margin-left:0!important}
    @media(max-width:720px){
      .quick-actions{grid-template-columns:repeat(2,minmax(0,1fr));padding:.7rem;gap:.45rem!important}
      .quick-actions>span{grid-column:1/-1}
      .quick-actions button{font-size:.68rem!important;min-height:40px}
      .quick-actions .utility{grid-column:1/-1}
      .search-shell{overflow:hidden}
      .topbar-actions{min-width:0}
      .freshness{max-width:58vw;white-space:normal;line-height:1.25}
    }
  `;
  document.head.appendChild(style);

  function readCache() {
    try { return JSON.parse(localStorage.getItem(CACHE_KEY) || "null"); }
    catch { return null; }
  }

  function writeCache(value) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify({ ...value, saved_at: Date.now() })); }
    catch { /* storage is optional */ }
  }

  async function fetchJson(path, attempts = 4) {
    let lastError;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        const response = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        return await response.json();
      } catch (error) {
        lastError = error;
        if (attempt + 1 < attempts) await new Promise(resolve => setTimeout(resolve, 500 * (2 ** attempt)));
      }
    }
    throw lastError;
  }

  function renderSummary(stats, health, cached = false) {
    if (stats) {
      $("#metric-active").textContent = formatNumber(stats.active_listings);
      $("#metric-companies").textContent = formatNumber(stats.companies);
      $("#metric-new").textContent = formatNumber(stats.new_today ?? stats.new_24h);
    }
    if (!health) return;
    const node = $("#freshness");
    const inventory = health.inventory || {};
    const fresh = Number(inventory.fresh || 0);
    const total = Number(inventory.total || 0);
    const unhealthy = Number(inventory.unhealthy || 0);
    const running = Number(inventory.running || 0);
    const degraded = Number(inventory.degraded || 0);
    const percent = total ? (100 * fresh / total).toFixed(1) : "0.0";
    const label = node?.querySelector("span:last-child");
    if (label) {
      label.textContent = unhealthy
        ? `${percent}% current · ${formatNumber(unhealthy)} catching up${running ? ` · ${running} crawling` : ""}${cached ? " · cached" : ""}`
        : `${formatNumber(total)} sources current${running ? ` · ${running} crawling` : ""}${cached ? " · cached" : ""}`;
    }
    if (node) {
      node.title = `${formatNumber(fresh)} of ${formatNumber(total)} validated sources are current. ${formatNumber(degraded)} degraded.`;
      node.classList.remove("failed", "fresh", "stale");
      node.classList.add(unhealthy === 0 ? "fresh" : "stale");
    }
  }

  async function recoverLiveSummary() {
    const cached = readCache();
    if (cached) renderSummary(cached.stats, cached.health, true);
    try {
      const [stats, health] = await Promise.all([fetchJson("/api/stats"), fetchJson("/api/health")]);
      writeCache({ stats, health });
      renderSummary(stats, health, false);
    } catch {
      const node = $("#freshness");
      if (!cached && node) {
        node.className = "freshness stale";
        const label = node.querySelector("span:last-child");
        if (label) label.textContent = "Live status delayed · retrying";
      }
    }
  }

  function applyPreset(preset) {
    const presets = {
      new: { "posted-within": "1", trust: "verified" },
      summer2027: { target: "exact", trust: "verified" },
      remote: { remote: true, trust: "verified" },
      quant: { category: "quant", target: "default", trust: "verified" },
      software: { category: "software", target: "default", trust: "verified" },
    };
    const values = presets[preset];
    if (!values) return;
    for (const [id, value] of Object.entries(values)) {
      const node = $(`#${id}`);
      if (!node) continue;
      if (node.type === "checkbox") node.checked = Boolean(value);
      else node.value = String(value);
    }
    ($("#trust") || $("#search"))?.dispatchEvent(new Event("change", { bubbles: true }));
    $("#results")?.scrollIntoView({ behavior: "smooth" });
  }

  async function copySearch() {
    try {
      await navigator.clipboard.writeText(location.href);
      $("#copy-search").textContent = "Copied";
      setTimeout(() => { $("#copy-search").textContent = "Copy search"; }, 1400);
    } catch { prompt("Copy this search URL", location.href); }
  }

  function csvCell(value) {
    const text = String(value ?? "");
    return `"${text.replaceAll('"', '""')}"`;
  }

  async function exportSaved() {
    let keys = [];
    let tracking = {};
    try {
      keys = JSON.parse(localStorage.getItem("gaia:saved") || "[]");
      tracking = JSON.parse(localStorage.getItem("gaia:tracking") || "{}");
    } catch { keys = []; }
    if (!keys.length) { alert("Save at least one role before exporting."); return; }
    const button = $("#export-saved");
    button.disabled = true;
    button.textContent = "Building export…";
    const rows = await Promise.all(keys.map(async key => {
      try {
        const item = await fetchJson(`/api/families/${encodeURIComponent(key)}?trust=all`, 2);
        const first = item.openings?.[0] || {};
        return [item.company, item.title, tracking[key] || "saved", first.apply_url || "", item.latest_posted_at || "", item.last_verified_at || ""];
      } catch { return ["", key, tracking[key] || "saved", "", "", ""]; }
    }));
    const csv = [["Company", "Role", "Status", "Application URL", "Employer posted", "Last checked"], ...rows]
      .map(row => row.map(csvCell).join(",")).join("\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    link.download = `gaia-saved-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
    button.disabled = false;
    button.textContent = "Export CSV";
  }

  document.addEventListener("click", event => {
    const preset = event.target.closest("[data-preset]");
    if (preset) applyPreset(preset.dataset.preset);
  });
  $("#copy-search")?.addEventListener("click", copySearch);
  $("#export-saved")?.addEventListener("click", exportSaved);
  recoverLiveSummary();
  setInterval(recoverLiveSummary, 15000);
})();
