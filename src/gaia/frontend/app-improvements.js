"use strict";

(() => {
  const $ = selector => document.querySelector(selector);
  const formatNumber = value => new Intl.NumberFormat().format(Number(value || 0));
  const CACHE_KEY = "gaia:live-summary";
  const REQUEST_TIMEOUT_MS = 8000;
  const HEALTHY_REFRESH_MS = 30000;
  const DEGRADED_REFRESH_MS = 10000;
  const MOBILE_QUERY = window.matchMedia("(max-width: 720px)");
  let recoveryTimer = null;
  let recoveryInFlight = null;

  const style = document.createElement("style");
  style.dataset.gaiaMobileProduct = "true";
  style.textContent = `
    .quick-actions{padding:.75rem .8rem .85rem;display:grid!important;grid-template-columns:repeat(3,minmax(0,1fr));gap:.5rem!important;margin:0!important;border-top:1px solid var(--line)}
    .quick-actions>span{grid-column:1/-1;margin:0!important;font-size:.62rem!important}
    .quick-actions button{width:100%;min-width:0;min-height:38px;padding:.45rem .55rem!important;font-size:.72rem!important;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .quick-actions .utility{margin-left:0!important}
    .mobile-filter-disclosure{display:contents}
    .mobile-filter-summary{display:none}
    @media(max-width:720px){
      body{padding-bottom:env(safe-area-inset-bottom)}
      .topbar{min-height:0!important;grid-template-columns:auto minmax(0,1fr) auto!important;gap:.35rem!important;padding:.35rem .75rem .25rem!important}
      .brand{font-size:.8rem}
      .topbar nav{grid-column:2;grid-row:1;justify-self:center!important;min-width:0;gap:0!important}
      .nav-link{min-height:42px;padding:.5rem .58rem!important;font-size:.74rem!important}
      .topbar-actions{display:contents!important}
      .theme-toggle{grid-column:3;grid-row:1;min-width:36px!important;width:36px!important;height:36px!important;padding:0!important}
      .freshness{grid-column:1/-1!important;grid-row:2;max-width:none!important;min-height:18px!important;padding:0 0 .1rem!important;font-size:.64rem!important;line-height:1.15!important;white-space:nowrap!important}
      main{padding:0 .75rem calc(6rem + env(safe-area-inset-bottom))!important}
      .page-intro{padding:1rem 0 .85rem!important}
      .page-intro h1{max-width:100%;font-size:clamp(2rem,8.6vw,2.55rem)!important;line-height:.98!important;letter-spacing:-.055em!important}
      .page-intro p{font-size:.78rem!important}
      .metrics{margin-top:.75rem!important;border-radius:10px}
      .metrics div{padding:.55rem .6rem!important}
      .metrics strong{font-size:1.08rem!important}
      .metrics span{font-size:.6rem!important;line-height:1.2}
      .search-shell{display:flex;flex-direction:column;margin-top:.75rem!important;overflow:hidden;border-radius:12px}
      .search-field{order:1;height:46px!important}
      .quick-actions{order:2!important;display:flex!important;grid-template-columns:none!important;flex-wrap:nowrap!important;overflow-x:auto;padding:.55rem .6rem!important;gap:.4rem!important;margin:0!important;border-top:0!important;border-bottom:1px solid var(--line);scroll-snap-type:x proximity;scrollbar-width:none;-webkit-overflow-scrolling:touch}
      .quick-actions::-webkit-scrollbar{display:none}
      .quick-actions>span{display:none!important}
      .quick-actions button{width:auto!important;min-width:max-content!important;flex:0 0 auto;min-height:36px!important;padding:.45rem .72rem!important;font-size:.68rem!important;scroll-snap-align:start}
      .quick-actions .utility{grid-column:auto!important;margin-left:0!important}
      .mobile-filter-disclosure{order:3;display:block;border:0;background:var(--surface)}
      .mobile-filter-summary{min-height:46px;display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.65rem .8rem;list-style:none;color:var(--ink);font-size:.72rem;font-weight:800;cursor:pointer;user-select:none}
      .mobile-filter-summary::-webkit-details-marker{display:none}
      .filter-summary-meta{display:flex;align-items:center;gap:.55rem;color:var(--muted);font-size:.65rem;font-weight:700}
      .mobile-filter-disclosure.has-active-filters .filter-summary-meta{color:var(--green)}
      .filter-chevron{width:.52rem;height:.52rem;border-right:2px solid currentColor;border-bottom:2px solid currentColor;transform:rotate(45deg) translateY(-2px);transition:transform .16s ease}
      .mobile-filter-disclosure[open] .filter-chevron{transform:rotate(225deg) translate(-1px,-1px)}
      .mobile-filter-disclosure:not([open])>.filter-grid{display:none!important}
      .mobile-filter-disclosure[open]>.filter-grid{display:grid!important}
      .filter-grid label{padding:.5rem .6rem!important}
      .filter-grid select,.filter-grid label>input[type="search"]{height:34px!important;font-size:.74rem!important}
      .results-head{padding:.9rem 0 .45rem!important}
      #result-note{display:none!important}
      .job-list{border-radius:12px}
      .job-row{padding:.72rem .75rem!important}
      .pagination{padding-bottom:calc(5.5rem + env(safe-area-inset-bottom))!important}
      .toast{bottom:calc(5.25rem + env(safe-area-inset-bottom))!important;max-width:calc(100% - 1.5rem);text-align:center}
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
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      try {
        const response = await fetch(path, {
          headers: { Accept: "application/json" },
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        const value = await response.json();
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid JSON response");
        return value;
      } catch (error) {
        lastError = error;
        if (attempt + 1 < attempts) await new Promise(resolve => setTimeout(resolve, 500 * (2 ** attempt)));
      } finally {
        clearTimeout(timeout);
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
    if (!health) return false;
    const node = $("#freshness");
    const inventory = health.inventory || {};
    const fresh = Number(inventory.fresh || 0);
    const total = Number(inventory.total || 0);
    const unhealthy = Number(inventory.unhealthy || 0);
    const running = Number(inventory.running || 0);
    const degraded = Number(inventory.degraded || 0);
    const percent = total ? (100 * fresh / total).toFixed(1) : "0.0";
    const truthfullyHealthy = !cached && health.ok === true && health.stale !== true && inventory.healthy !== false && unhealthy === 0 && total > 0;
    const label = node?.querySelector("span:last-child");
    if (label) {
      if (cached) {
        label.textContent = `${formatNumber(fresh)} / ${formatNumber(total)} last known current · cached snapshot`;
      } else if (truthfullyHealthy) {
        label.textContent = `${formatNumber(total)} sources current${running ? ` · ${running} crawling` : ""}`;
      } else {
        label.textContent = `${percent}% current · ${formatNumber(unhealthy)} unhealthy${running ? ` · ${running} crawling` : ""}`;
      }
    }
    if (node) {
      node.title = cached
        ? `Last known status only. ${formatNumber(fresh)} of ${formatNumber(total)} sources were current when cached.`
        : `${formatNumber(fresh)} of ${formatNumber(total)} validated sources are current. ${formatNumber(degraded)} degraded.`;
      node.classList.remove("failed", "fresh", "stale");
      node.classList.add(truthfullyHealthy ? "fresh" : "stale");
    }
    return truthfullyHealthy;
  }

  function scheduleRecovery(delay) {
    clearTimeout(recoveryTimer);
    recoveryTimer = setTimeout(() => {
      recoveryTimer = null;
      if (!document.hidden) recoverLiveSummary();
    }, delay);
  }

  async function recoverLiveSummary() {
    if (recoveryInFlight) return recoveryInFlight;
    recoveryInFlight = (async () => {
      const cached = readCache();
      if (cached) renderSummary(cached.stats, cached.health, true);
      try {
        const [stats, health] = await Promise.all([fetchJson("/api/stats"), fetchJson("/api/health")]);
        const healthy = renderSummary(stats, health, false);
        if (healthy) writeCache({ stats, health });
        scheduleRecovery(healthy ? HEALTHY_REFRESH_MS : DEGRADED_REFRESH_MS);
      } catch {
        const node = $("#freshness");
        if (!cached && node) {
          node.className = "freshness stale";
          const label = node.querySelector("span:last-child");
          if (label) label.textContent = "Live status delayed · retrying";
        }
        scheduleRecovery(DEGRADED_REFRESH_MS);
      }
    })().finally(() => { recoveryInFlight = null; });
    return recoveryInFlight;
  }

  function installMobileFilterDisclosure() {
    const grid = $(".filter-grid");
    if (!grid || grid.closest(".mobile-filter-disclosure")) return;

    const details = document.createElement("details");
    details.className = "mobile-filter-disclosure";
    details.id = "advanced-filters";
    details.dataset.mobileOpen = "false";

    const summary = document.createElement("summary");
    summary.className = "mobile-filter-summary";
    summary.innerHTML = '<span>Filters</span><span class="filter-summary-meta"><span id="active-filter-count">All jobs</span><span class="filter-chevron" aria-hidden="true"></span></span>';

    grid.parentNode.insertBefore(details, grid);
    details.append(summary, grid);

    const defaults = {
      trust: "all",
      category: "",
      company: "",
      location: "",
      target: "",
      "posted-within": "0",
      sort: "newest",
      remote: false,
    };

    const updateCount = () => {
      let count = 0;
      for (const [id, defaultValue] of Object.entries(defaults)) {
        const control = document.getElementById(id);
        if (!control) continue;
        const value = control.type === "checkbox" ? control.checked : control.value;
        if (value !== defaultValue) count += 1;
      }
      const countNode = document.getElementById("active-filter-count");
      if (countNode) countNode.textContent = count ? `${count} active` : "All jobs";
      details.classList.toggle("has-active-filters", count > 0);
      summary.setAttribute("aria-label", count ? `Filters, ${count} active` : "Filters, none active");
    };

    const syncViewport = event => {
      if (event.matches) {
        details.open = details.dataset.mobileOpen === "true";
      } else {
        details.open = true;
      }
    };

    details.addEventListener("toggle", () => {
      if (MOBILE_QUERY.matches) details.dataset.mobileOpen = String(details.open);
    });
    grid.addEventListener("change", updateCount);
    grid.addEventListener("input", updateCount);
    $("#clear-filters")?.addEventListener("click", () => setTimeout(updateCount, 0));
    document.addEventListener("click", event => {
      if (event.target.closest("[data-preset]")) setTimeout(updateCount, 0);
    });
    if (MOBILE_QUERY.addEventListener) MOBILE_QUERY.addEventListener("change", syncViewport);
    else MOBILE_QUERY.addListener(syncViewport);

    syncViewport(MOBILE_QUERY);
    updateCount();
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
    $("#results")?.scrollIntoView({ behavior: "smooth", block: "start" });
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
    try {
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
    } finally {
      button.disabled = false;
      button.textContent = "Export CSV";
    }
  }

  function loadOutageController() {
    const alreadyLoaded = [...document.scripts].some(script => script.src.includes("/assets/outage-controller.js"));
    if (alreadyLoaded) return;
    const script = document.createElement("script");
    script.src = "/assets/outage-controller.js?v=1.2.1";
    script.dataset.gaiaOutageController = "true";
    script.async = true;
    document.head.appendChild(script);
  }

  document.addEventListener("click", event => {
    const preset = event.target.closest("[data-preset]");
    if (preset) applyPreset(preset.dataset.preset);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(recoveryTimer);
    else recoverLiveSummary();
  });
  window.addEventListener("online", recoverLiveSummary);
  $("#copy-search")?.addEventListener("click", copySearch);
  $("#export-saved")?.addEventListener("click", exportSaved);
  installMobileFilterDisclosure();
  loadOutageController();
  recoverLiveSummary();
})();
