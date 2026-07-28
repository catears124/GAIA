"use strict";

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>'"]/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[character]));
const formatNumber = value => new Intl.NumberFormat().format(Number(value || 0));
const DEFAULTS = {
  q: "",
  category: "",
  target: "",
  trust: "all",
  company: "",
  location: "",
  remote: false,
  posted_within: "0",
  sort: "newest",
};

const state = {
  page: 1,
  pageSize: 48,
  total: 0,
  items: [],
  controller: null,
  view: "jobs",
  coverage: null,
  universe: null,
  healthTimer: null,
  inventoryActivity: undefined,
};

function readStorage(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key) || JSON.stringify(fallback)); }
  catch { return fallback; }
}

const savedSet = () => new Set(readStorage("gaia:saved", []));
const trackingMap = () => readStorage("gaia:tracking", {});

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}

function relative(value, precision = "timestamp") {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  const minutes = Math.max(0, Math.round((Date.now() - date.getTime()) / 60000));
  if (precision === "day") return `~${Math.max(1, Math.round(minutes / 1440))}d ago`;
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days < 60 ? `${days}d ago` : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function exact(value) {
  if (!value) return "Not provided";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not provided" : date.toLocaleString();
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { Accept: "application/json" }, ...options });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 1800);
}

function formState() {
  return {
    q: $("#search").value.trim(),
    category: $("#category").value,
    target: $("#target").value,
    trust: $("#trust").value,
    company: $("#company").value,
    location: $("#location").value.trim(),
    remote: $("#remote").checked,
    posted_within: $("#posted-within").value,
    sort: $("#sort").value,
  };
}

function queryParams() {
  const values = formState();
  const params = new URLSearchParams();
  if (state.page > 1) params.set("page", String(state.page));
  for (const [key, value] of Object.entries(values)) {
    if (value !== DEFAULTS[key]) params.set(key, String(value));
  }
  return params;
}

function queryUrl(path) {
  const query = queryParams().toString();
  return `${path}${query ? `?${query}` : ""}`;
}

function hasFilters() {
  const values = formState();
  return Object.entries(values).some(([key, value]) => value !== DEFAULTS[key]);
}

function skeletons(target = "#job-grid", count = 10) {
  $(target).innerHTML = Array.from({ length: count }, () => '<div class="skeleton" aria-hidden="true"></div>').join("");
  $(target).setAttribute("aria-busy", "true");
}

async function loadStats() {
  try {
    const data = await api("/api/stats");
    $("#metric-active").textContent = formatNumber(data.active_listings);
    $("#metric-companies").textContent = formatNumber(data.companies);
    $("#metric-new").textContent = formatNumber(data.new_today);
  } catch {
    $$(".metrics strong").forEach(node => { node.textContent = "—"; });
  }
}

async function loadFacets() {
  try {
    const params = new URLSearchParams();
    if ($("#trust").value !== DEFAULTS.trust) params.set("trust", $("#trust").value);
    if ($("#target").value !== DEFAULTS.target) params.set("target", $("#target").value);
    const suffix = params.toString();
    const data = await api(`/api/facets${suffix ? `?${suffix}` : ""}`);
    const selected = $("#company").value;
    $("#company").innerHTML = '<option value="">All companies</option>' +
      data.companies.map(item => `<option value="${esc(item.value)}">${esc(item.value)} (${formatNumber(item.count)})</option>`).join("");
    $("#company").value = selected;
  } catch {
    // The main list does not depend on facets.
  }
}

async function loadJobs() {
  state.controller?.abort();
  state.controller = new AbortController();
  skeletons();
  $("#empty-state").hidden = true;
  const url = queryUrl("/api/families");
  history.replaceState(null, "", queryUrl(location.pathname));
  try {
    const data = await api(url, { signal: state.controller.signal });
    state.items = data.items;
    state.total = data.total;
    renderJobs();
  } catch (error) {
    if (error.name === "AbortError") return;
    $("#job-grid").innerHTML = "";
    $("#job-grid").setAttribute("aria-busy", "false");
    showEmpty("Could not load jobs.", "The inventory API did not respond. Try again in a moment.", true);
  }
}

function sourceBadge(item) {
  return item.verified
    ? '<span class="badge verified">Employer</span>'
    : '<span class="badge lead">Lead</span>';
}

function jobRow(item) {
  const saved = savedSet().has(item.family_key);
  const tracked = trackingMap()[item.family_key];
  const locations = item.locations || [];
  const location = locations.slice(0, 2).join(" · ") || "Location not stated";
  const primaryDate = item.latest_posted_at
    ? `Posted ${relative(item.latest_posted_at, item.posted_precision)}`
    : `Found ${relative(item.first_detected_at)}`;
  const secondaryDate = item.latest_posted_at
    ? `Found ${relative(item.first_detected_at)}`
    : `Checked ${relative(item.last_verified_at)}`;
  const cycle = item.year ? (item.season ? `${item.season} ${item.year}` : String(item.year)) : "Cycle not stated";
  return `<article class="job-row" role="listitem" data-key="${esc(item.family_key)}">
    <div class="job-date"><strong title="${esc(exact(item.latest_posted_at || item.first_detected_at))}">${esc(primaryDate)}</strong><span>${esc(secondaryDate)}</span></div>
    <div class="job-role"><button data-open="${esc(item.family_key)}">${esc(item.title || "Untitled internship")}</button><span>${esc(item.company || "Unknown company")} · ${esc(cycle)}</span></div>
    <div class="job-location" title="${esc(locations.join(" · "))}">${esc(location)}${locations.length > 2 ? ` +${locations.length - 2}` : ""}</div>
    <div class="job-source">${sourceBadge(item)}<span class="tag">${esc(item.category || "technical")}</span><span class="tag">${formatNumber(item.opening_count)} opening${item.opening_count === 1 ? "" : "s"}</span>${tracked ? `<span class="tag">${esc(tracked)}</span>` : ""}</div>
    <div class="row-actions"><button class="icon-button ${saved ? "saved" : ""}" data-save="${esc(item.family_key)}" aria-label="${saved ? "Remove saved job" : "Save job"}">${saved ? "♥" : "♡"}</button><button class="open-button" data-open="${esc(item.family_key)}">Open</button></div>
  </article>`;
}

function renderJobs() {
  $("#job-grid").innerHTML = state.items.map(jobRow).join("");
  $("#job-grid").setAttribute("aria-busy", "false");
  const trust = $("#trust").value;
  const label = trust === "verified" ? "verified jobs" : trust === "leads" ? "unverified leads" : "jobs and leads";
  $("#result-count").textContent = `${formatNumber(state.total)} ${label}`;
  $("#clear-filters").hidden = !hasFilters();
  if (!state.items.length) showEmpty("No matching jobs.", "Remove a filter or include all confidence levels.");
  const start = state.total ? (state.page - 1) * state.pageSize + 1 : 0;
  const end = Math.min(state.total, state.page * state.pageSize);
  $("#page-label").textContent = `${formatNumber(start)}–${formatNumber(end)} of ${formatNumber(state.total)}`;
  $("#prev").disabled = state.page <= 1;
  $("#next").disabled = end >= state.total;
  updateSavedCount();
}

function showEmpty(title, detail, retry = false) {
  const node = $("#empty-state");
  node.hidden = false;
  node.innerHTML = `<strong>${esc(title)}</strong><p>${esc(detail)}</p>${retry ? '<button class="primary" data-retry>Try again</button>' : ""}`;
}

function toggleSaved(key) {
  const saved = savedSet();
  const adding = !saved.has(key);
  if (adding) saved.add(key); else saved.delete(key);
  localStorage.setItem("gaia:saved", JSON.stringify([...saved]));
  updateSavedCount();
  renderJobs();
  if (state.view === "saved") loadSaved();
  toast(adding ? "Saved" : "Removed from saved");
}

function updateSavedCount() {
  $("#saved-count").textContent = savedSet().size;
}

function setTracking(key, value) {
  const tracking = trackingMap();
  if (value) tracking[key] = value; else delete tracking[key];
  localStorage.setItem("gaia:tracking", JSON.stringify(tracking));
  renderJobs();
  toast(value ? `Marked ${value}` : "Tracking cleared");
}

async function openFamily(key) {
  const drawer = $("#drawer");
  $("#drawer-content").innerHTML = '<div class="drawer-body"><div class="skeleton"></div></div>';
  drawer.showModal();
  try {
    const trust = $("#trust").value;
    const item = await api(`/api/families/${encodeURIComponent(key)}?trust=${encodeURIComponent(trust)}`);
    const currentStatus = trackingMap()[key] || "";
    const options = [
      ["", "Not tracked"], ["applied", "Applied"], ["interview", "Interviewing"],
      ["offer", "Offer"], ["rejected", "Rejected"], ["ignored", "Not interested"],
    ].map(([value, label]) => `<option value="${value}" ${value === currentStatus ? "selected" : ""}>${label}</option>`).join("");
    const openings = (item.openings || []).map((opening, index) => {
      const lead = ["registry", "external-index", "verification-lead"].includes(opening.source_mode);
      const timing = opening.posted_at ? `Employer posted ${exact(opening.posted_at)}` : `First found ${exact(opening.first_detected_at)}`;
      return `<article class="opening"><div class="opening-head"><div><strong>Opening ${index + 1}</strong><p>${esc((opening.location || []).join(" · ") || "Location not stated")}</p><small>${esc(timing)} · ${lead ? "Unverified lead" : "Direct employer application"} · ${esc(opening.source || "Unknown source")}</small></div><a class="apply-link" href="${esc(safeUrl(opening.apply_url))}" target="_blank" rel="noopener noreferrer">View and apply →</a></div></article>`;
    }).join("");
    const trustNote = item.verified
      ? "This role was recovered from an employer-controlled ATS or jobs API."
      : "This is a market lead. GAIA has not independently recovered the employer application yet.";
    $("#drawer-content").innerHTML = `<div class="drawer-body">
      <span class="badge ${item.verified ? "verified" : "lead"}">${item.verified ? "Employer verified" : "Lead"}</span>
      <h2 id="drawer-title">${esc(item.title)}</h2>
      <div class="drawer-meta"><span class="tag">${esc(item.company)}</span><span class="tag">${esc(item.category)}</span><span class="tag">${formatNumber(item.opening_count)} opening${item.opening_count === 1 ? "" : "s"}</span></div>
      <div class="trust-note ${item.verified ? "" : "lead"}">${esc(trustNote)}</div>
      <div class="fact-grid"><div><span>Employer posted</span><strong>${item.latest_posted_at ? esc(exact(item.latest_posted_at)) : "Not published"}</strong></div><div><span>First detected</span><strong>${esc(exact(item.first_detected_at))}</strong></div><div><span>Last checked</span><strong>${esc(exact(item.last_verified_at))}</strong></div></div>
      <label class="tracking-field"><span>Application status</span><select id="tracking-status" data-family="${esc(key)}">${options}</select></label>
      <h3>Applications</h3>${openings || "<p>No application URL is currently available.</p>"}
    </div>`;
  } catch {
    $("#drawer-content").innerHTML = '<div class="drawer-body"><h2>Could not load this role.</h2><p>Close the panel and try again.</p></div>';
  }
}

async function loadSaved() {
  const keys = [...savedSet()];
  const grid = $("#saved-grid");
  const empty = $("#saved-empty");
  if (!keys.length) {
    grid.innerHTML = "";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  skeletons("#saved-grid", Math.min(keys.length, 8));
  const items = (await Promise.all(keys.map(key => api(`/api/families/${encodeURIComponent(key)}?trust=all`).catch(() => null)))).filter(Boolean);
  grid.innerHTML = items.map(jobRow).join("");
  grid.setAttribute("aria-busy", "false");
  empty.hidden = items.length > 0;
}

function isActionable(source) {
  return (source.scope || "current") === "current" &&
    ["board", "board-search", "domain"].includes(source.mode) &&
    (source.last_error || ["broken", "truncated", "empty"].includes(source.status));
}

function matchesCoverage(source, filter) {
  if (filter === "all") return true;
  if (filter === "actionable") return isActionable(source);
  if (filter === "access") return source.status === "blocked";
  if (filter === "stale") return ["stale", "unstructured", "dormant"].includes(source.status);
  return source.scope === "historical";
}

function evidenceLabel(types = []) {
  const labels = {
    "historical-internship": "past internship archive",
    "current-index": "current public index",
    "market-index": "market signal",
    "employer-page": "employer careers page",
    "employer-page-lead": "employer page lead",
    "historical-direct": "past direct application",
  };
  return types.map(type => labels[type] || type).join(" · ");
}

function renderUniverse() {
  const universe = state.universe || { ready: false, summary: {}, frontier: [] };
  const coverage = state.coverage || { summary: {}, contract: {}, sources: [] };
  const summary = universe.summary || {};
  if (!universe.ready) {
    $("#universe-summary").innerHTML = '<div class="universe-stat"><strong>Building</strong><span>Employer census will appear after reconciliation.</span></div>';
  } else {
    $("#universe-summary").innerHTML = [
      [summary.known_employers, "known plausible employers"],
      [summary.enumerated_employers, "employers independently enumerated"],
      [summary.unresolved_employers, "employers still unresolved"],
      [summary.blind_spots, "non-registry blind spots"],
    ].map(([value, label]) => `<article class="universe-stat"><strong>${formatNumber(value)}</strong><span>${esc(label)}</span></article>`).join("");
  }
  const benchmark = coverage.summary || {};
  $("#coverage-benchmark").textContent = benchmark.registry_floor
    ? `Benchmark cross-check: ${formatNumber(benchmark.independent_matches)} / ${formatNumber(benchmark.registry_floor)} independently recovered (${benchmark.registry_recall_percent}%)`
    : "Benchmark cross-check unavailable";
  $("#coverage-source-count").textContent = `${formatNumber(summary.validated_sources || 0)} validated source records · ${formatNumber(summary.candidate_surfaces || 0)} candidate surfaces`;

  const frontier = universe.frontier || [];
  $("#frontier-count").textContent = `${formatNumber(frontier.length)} shown`;
  $("#frontier-list").innerHTML = frontier.length ? frontier.map(item => {
    const years = (item.historical_years || []).join(", ");
    const evidence = evidenceLabel(item.evidence_types || []);
    return `<article class="frontier-row"><div><strong>${esc(item.canonical_name)}</strong><small>${item.blind_spot ? "Outside current registry coverage" : esc(item.resolution_status)}${years ? ` · historical years ${esc(years)}` : ""}</small></div><div class="frontier-evidence" title="${esc(evidence)}">${esc(evidence || "Unresolved employer evidence")}</div><div class="frontier-score"><strong>${Number(item.frontier_score || 0).toFixed(1)}</strong><small>frontier score</small></div></article>`;
  }).join("") : '<div class="empty-state"><strong>No unresolved employers.</strong><p>The employer frontier is empty.</p></div>';

  const sources = (coverage.sources || []).filter(source => matchesCoverage(source, $("#coverage-filter").value));
  $("#coverage-list").innerHTML = sources.length ? sources.map(source => {
    const bad = isActionable(source);
    const warn = ["blocked", "stale", "dormant", "empty"].includes(source.status);
    return `<article class="source-row"><strong title="${esc(source.source)}">${esc(source.source)}</strong><span>${esc(source.mode)} · ${esc(source.scope)}</span><span>${formatNumber(source.rows_scanned)} rows</span><span class="source-status ${bad ? "bad" : warn ? "warn" : ""}">${esc(source.status)}</span></article>`;
  }).join("") : '<div class="empty-state"><strong>No source records match.</strong></div>';
}

async function loadUniverse() {
  try {
    [state.coverage, state.universe] = await Promise.all([api("/api/coverage"), api("/api/universe?limit=100")]);
    renderUniverse();
  } catch {
    $("#frontier-list").innerHTML = '<div class="empty-state"><strong>Employer census unavailable.</strong><p>The coverage API did not respond.</p></div>';
  }
}

function setView(view) {
  state.view = view;
  $$(".product-view").forEach(node => { node.hidden = node.id !== `${view}-view`; });
  $$(".nav-link").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  if (view === "saved") loadSaved();
  if (view === "universe") loadUniverse();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function refreshHealth() {
  clearTimeout(state.healthTimer);
  try {
    const data = await api("/api/health");
    const inventory = data.inventory || {};
    const fresh = Number(inventory.fresh || 0);
    const total = Number(inventory.total || 0);
    const unhealthy = Number(inventory.unhealthy || 0);
    const activity = inventory.latest_activity_at || null;
    const node = $("#freshness");
    node.className = `freshness ${data.ok ? "fresh" : unhealthy ? "stale" : "fresh"}`;
    node.lastElementChild.textContent = data.running
      ? `${formatNumber(fresh)} / ${formatNumber(total)} validated sources current · crawling`
      : data.ok
        ? `${formatNumber(total)} validated sources current`
        : `${formatNumber(fresh)} / ${formatNumber(total)} validated sources current`;
    if (state.inventoryActivity !== undefined && state.inventoryActivity !== activity) {
      await Promise.all([loadJobs(), loadStats()]);
      if (state.view === "universe") await loadUniverse();
    }
    state.inventoryActivity = activity;
    state.healthTimer = setTimeout(refreshHealth, data.running || !data.ok ? 10000 : 30000);
  } catch {
    const node = $("#freshness");
    node.className = "freshness failed";
    node.lastElementChild.textContent = "Inventory unavailable";
    state.healthTimer = setTimeout(refreshHealth, 10000);
  }
}

function clearFilters() {
  $("#search").value = DEFAULTS.q;
  $("#category").value = DEFAULTS.category;
  $("#target").value = DEFAULTS.target;
  $("#trust").value = DEFAULTS.trust;
  $("#company").value = DEFAULTS.company;
  $("#location").value = DEFAULTS.location;
  $("#remote").checked = DEFAULTS.remote;
  $("#posted-within").value = DEFAULTS.posted_within;
  $("#sort").value = DEFAULTS.sort;
  state.page = 1;
  Promise.all([loadFacets(), loadJobs()]);
}

const initial = new URLSearchParams(location.search);
const fieldMap = {
  q: "search",
  category: "category",
  target: "target",
  trust: "trust",
  company: "company",
  location: "location",
  posted_within: "posted-within",
  sort: "sort",
};
for (const [key, id] of Object.entries(fieldMap)) {
  if (initial.has(key)) $(`#${id}`).value = initial.get(key);
}
$("#remote").checked = initial.get("remote") === "true";
state.page = Math.max(1, Number(initial.get("page") || 1));

let searchTimer;
for (const selector of ["#search", "#location"]) {
  $(selector).addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.page = 1; loadJobs(); }, 220);
  });
}
for (const selector of ["#trust", "#category", "#company", "#target", "#sort", "#posted-within"]) {
  $(selector).addEventListener("change", () => {
    state.page = 1;
    if (["#trust", "#target"].includes(selector)) loadFacets();
    loadJobs();
  });
}
$("#remote").addEventListener("change", () => { state.page = 1; loadJobs(); });
$("#clear-filters").addEventListener("click", clearFilters);
$("#prev").addEventListener("click", () => { state.page -= 1; loadJobs(); $("#results").scrollIntoView(); });
$("#next").addEventListener("click", () => { state.page += 1; loadJobs(); $("#results").scrollIntoView(); });
$("#drawer-close").addEventListener("click", () => $("#drawer").close());
$("#drawer").addEventListener("click", event => { if (event.target === $("#drawer")) $("#drawer").close(); });
$("#drawer").addEventListener("change", event => {
  if (event.target.matches("#tracking-status")) setTracking(event.target.dataset.family, event.target.value);
});
$("#coverage-filter").addEventListener("change", renderUniverse);

document.addEventListener("click", event => {
  const save = event.target.closest("[data-save]");
  const open = event.target.closest("[data-open]");
  const view = event.target.closest("[data-view]");
  if (save) toggleSaved(save.dataset.save);
  else if (open) openFamily(open.dataset.open);
  else if (view) setView(view.dataset.view);
  else if (event.target.matches("[data-retry]")) loadJobs();
});

document.addEventListener("keydown", event => {
  if (event.key === "/" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) {
    event.preventDefault();
    $("#search").focus();
  }
  if (event.key === "Escape" && $("#drawer").open) $("#drawer").close();
});

updateSavedCount();
skeletons();
Promise.all([loadFacets(), loadJobs(), loadStats(), refreshHealth()]);
