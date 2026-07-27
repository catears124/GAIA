"use strict";

const state = {
  page: 1,
  pageSize: 48,
  total: 0,
  items: [],
  controller: null,
  coverage: null,
  view: "jobs",
  healthTimer: null,
  wasRunning: false,
};

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>'"]/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[character]));
const formatNumber = value => new Intl.NumberFormat().format(Number(value || 0));

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
  if (!value) return "Not provided by employer";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not provided by employer" : date.toLocaleString();
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
  toast.timer = setTimeout(() => node.classList.remove("show"), 1900);
}

function monogram(company = "") {
  return company.split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]).join("").toUpperCase() || "•";
}

function sourceBadge(item) {
  return item.verified
    ? '<span class="badge verified">Employer verified</span>'
    : '<span class="badge lead">Lead to verify</span>';
}

function queryString() {
  return new URLSearchParams({
    page: state.page,
    page_size: state.pageSize,
    q: $("#search").value.trim(),
    category: $("#category").value,
    target: $("#target").value,
    trust: $("#trust").value,
    company: $("#company").value,
    location: $("#location").value.trim(),
    remote: $("#remote").checked,
    posted_within: $("#posted-within").value,
    sort: $("#sort").value,
  }).toString();
}

function hasFilters() {
  return Boolean(
    $("#search").value || $("#category").value || $("#company").value ||
    $("#location").value || $("#remote").checked || $("#posted-within").value !== "0" ||
    $("#target").value !== "default" || $("#trust").value !== "verified" ||
    $("#sort").value !== "newest"
  );
}

async function loadStats() {
  try {
    const data = await api("/api/stats");
    $("#metric-active").textContent = formatNumber(data.active_listings);
    $("#metric-companies").textContent = formatNumber(data.companies);
    $("#metric-new").textContent = formatNumber(data.new_24h);
  } catch {
    $$(".proof strong").forEach(node => { node.textContent = "—"; });
  }
}

function skeletons(target = "#job-grid", count = 9) {
  $(target).innerHTML = Array.from({ length: count }, () => '<div class="skeleton" aria-hidden="true"></div>').join("");
  $(target).setAttribute("aria-busy", "true");
}

async function loadFacets() {
  try {
    const params = new URLSearchParams({ trust: $("#trust").value, target: $("#target").value });
    const data = await api(`/api/facets?${params}`);
    const selected = $("#company").value || new URLSearchParams(location.search).get("company") || "";
    $("#company").innerHTML = '<option value="">All companies</option>' +
      data.companies.map(item => `<option value="${esc(item.value)}">${esc(item.value)} (${formatNumber(item.count)})</option>`).join("");
    $("#company").value = selected;
  } catch {
    // Company filtering is optional; the main feed remains available.
  }
}

async function loadJobs() {
  state.controller?.abort();
  state.controller = new AbortController();
  skeletons();
  $("#empty-state").hidden = true;
  try {
    history.replaceState(null, "", `${location.pathname}?${queryString()}`);
    const data = await api(`/api/families?${queryString()}`, { signal: state.controller.signal });
    state.items = data.items;
    state.total = data.total;
    renderJobs();
  } catch (error) {
    if (error.name === "AbortError") return;
    $("#job-grid").innerHTML = "";
    $("#job-grid").setAttribute("aria-busy", "false");
    showEmpty("The index is taking a breather.", "We could not reach the opportunities service. Try again in a moment.", true);
  }
}

function jobCard(item) {
  const saved = savedSet().has(item.family_key);
  const status = trackingMap()[item.family_key];
  const locations = item.locations || [];
  const location = locations.slice(0, 2).join(" · ") || "Location not stated";
  const dateValue = item.latest_posted_at || item.first_detected_at;
  const date = item.latest_posted_at
    ? `Posted ${relative(item.latest_posted_at, item.posted_precision)}`
    : `Found ${relative(item.first_detected_at)}`;
  const title = item.title || "Untitled internship";
  const company = item.company || "Unknown company";

  return `<article class="job-card" data-key="${esc(item.family_key)}">
    <div class="card-top">
      <div class="company"><span class="company-monogram">${esc(monogram(company))}</span><div><strong>${esc(company)}</strong>${sourceBadge(item)}</div></div>
      <button class="save-button ${saved ? "saved" : ""}" data-save="${esc(item.family_key)}" aria-label="${saved ? "Remove from saved" : "Save internship"}" title="${saved ? "Remove from saved" : "Save internship"}">${saved ? "♥" : "♡"}</button>
    </div>
    <h3><button data-open="${esc(item.family_key)}">${esc(title)}</button></h3>
    <div class="card-tags">
      <span class="tag">${esc(item.category || "technical")}</span>
      <span class="tag">${formatNumber(item.opening_count)} ${item.opening_count === 1 ? "opening" : "openings"}</span>
      ${status ? `<span class="tag">${esc(status)}</span>` : ""}
    </div>
    <div class="card-footer">
      <p><strong title="${esc(exact(dateValue))}">${esc(date)}</strong>${esc(location)}${locations.length > 2 ? ` +${locations.length - 2}` : ""}</p>
      <button class="open-button" data-open="${esc(item.family_key)}">View role →</button>
    </div>
  </article>`;
}

function renderJobs() {
  $("#job-grid").innerHTML = state.items.map(jobCard).join("");
  $("#job-grid").setAttribute("aria-busy", "false");
  const trust = $("#trust").value;
  const label = trust === "verified" ? "verified opportunities" : trust === "leads" ? "leads to verify" : "opportunities";
  $("#result-count").textContent = `${formatNumber(state.total)} ${label}`;
  $("#result-note").textContent = trust === "verified"
    ? "Direct applications recovered from employer systems."
    : trust === "leads"
      ? "Useful signals that still need employer confirmation."
      : "Every opportunity shows its source confidence.";
  $("#clear-filters").hidden = !hasFilters();

  if (!state.items.length) {
    showEmpty(
      "No roles match those filters.",
      trust === "verified" ? "Broaden your search or include clearly labeled leads." : "Try removing a filter."
    );
  }

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
  toast(adding ? "Saved to your shortlist" : "Removed from saved");
  if (state.view === "saved") loadSaved();
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
      const lead = ["registry", "external-index"].includes(opening.source_mode);
      const variants = (opening.source_variants || []).join(" · ");
      const timestamp = opening.posted_at || opening.first_detected_at;
      const timing = opening.posted_at ? `Employer posted ${exact(opening.posted_at)}` : `First found ${exact(opening.first_detected_at)}`;
      const source = opening.source || "Unknown source";
      const url = safeUrl(opening.apply_url);
      return `<article class="opening"><div class="opening-head"><div>
        <strong>Opening ${index + 1}</strong>
        <p>${esc((opening.location || []).join(" · ") || "Location not stated")}</p>
        <small>${esc(timing)} · ${lead ? "Index lead" : "Direct employer application"} · ${esc(source)}${variants ? ` · ${esc(variants)}` : ""}</small>
      </div><a class="apply-link" href="${esc(url)}" title="${esc(exact(timestamp))}" target="_blank" rel="noopener noreferrer">View &amp; apply →</a></div></article>`;
    }).join("");

    const trustNote = item.verified
      ? "This application came from the employer’s ATS or native jobs API. Check the employer page for final eligibility and availability."
      : "This was found in a public index but has not yet been recovered from an employer source. Treat it as a lead and verify before applying.";

    $("#drawer-content").innerHTML = `<div class="drawer-body">
      <p class="eyebrow"><span></span>${esc(item.company)} · ${item.verified ? "Verified" : "Lead"}</p>
      <h2 id="drawer-title">${esc(item.title)}</h2>
      <div class="drawer-meta"><span class="tag">${esc(item.category)}</span><span class="tag">${formatNumber(item.opening_count)} openings</span><span class="tag">${formatNumber(item.location_count)} locations</span></div>
      <div class="trust-note ${item.verified ? "" : "lead"}">${esc(trustNote)}</div>
      <div class="fact-grid">
        <div><span>Employer posted</span><strong>${item.latest_posted_at ? esc(exact(item.latest_posted_at)) : "Not published"}</strong></div>
        <div><span>First detected</span><strong>${esc(exact(item.first_detected_at))}</strong></div>
        <div><span>Last checked</span><strong>${esc(exact(item.last_verified_at))}</strong></div>
      </div>
      <label class="tracking-field"><span>Application status</span><select id="tracking-status" data-family="${esc(key)}">${options}</select></label>
      <h3>Applications</h3>
      ${openings || "<p>No application URL is currently available.</p>"}
    </div>`;
  } catch {
    $("#drawer-content").innerHTML = '<div class="drawer-body"><h2>We could not load this role.</h2><p>Close the panel and try again.</p></div>';
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
  skeletons("#saved-grid", Math.min(keys.length, 6));
  const results = await Promise.all(keys.map(key => api(`/api/families/${encodeURIComponent(key)}?trust=all`).catch(() => null)));
  const items = results.filter(Boolean);
  grid.innerHTML = items.map(jobCard).join("");
  grid.setAttribute("aria-busy", "false");
  empty.hidden = items.length > 0;
}

function setView(view) {
  state.view = view;
  $$(".product-view").forEach(node => { node.hidden = node.id !== `${view}-view`; });
  $$(".nav-link").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  $(".hero").hidden = view !== "jobs";
  if (view === "saved") loadSaved();
  if (view === "coverage") loadCoverage();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function isActionable(source) {
  return (source.scope || "current") === "current" &&
    ["board", "board-search", "domain"].includes(source.mode) &&
    (source.last_error || ["broken", "truncated"].includes(source.status));
}

function matchesCoverage(source, filter) {
  if (filter === "all") return true;
  if (filter === "actionable") return isActionable(source);
  if (filter === "access") return source.status === "blocked";
  if (filter === "stale") return ["stale", "unstructured"].includes(source.status);
  return source.scope === "historical";
}

function renderCoverage() {
  if (!state.coverage) return;
  const { summary, contract, sources } = state.coverage;
  $("#coverage-summary").innerHTML = [
    [summary.direct_applications, "direct applications"],
    [`${summary.direct_date_coverage_percent}%`, "employer-dated"],
    [summary.productive_direct_sources, "productive sources"],
    [contract.actionable_anomalies, "source issues"],
  ].map(([value, label]) => `<article class="coverage-stat"><strong>${esc(value)}</strong><span>${esc(label)}</span></article>`).join("");

  const rows = sources.filter(source => matchesCoverage(source, $("#coverage-filter").value));
  $("#coverage-source-count").textContent = `${formatNumber(rows.length)} sources`;
  $("#coverage-benchmark").textContent = `${summary.registry_recall_percent}% benchmark recall · ${summary.independent_matches}/${summary.registry_floor} matches`;
  $("#coverage-list").innerHTML = rows.length
    ? rows.map(source => {
      const bad = isActionable(source);
      const warn = ["blocked", "stale", "dormant", "empty"].includes(source.status);
      return `<article class="source-row">
        <strong title="${esc(source.source)}">${esc(source.source)}</strong>
        <span>${esc(source.mode)} · ${esc(source.scope)}</span>
        <span>${formatNumber(source.rows_scanned)} scanned</span>
        <span>${source.last_attempt_at ? esc(relative(source.last_attempt_at)) : "Never"}</span>
        <span class="source-status ${bad ? "bad" : warn ? "warn" : ""}">${esc(source.status)}</span>
      </article>`;
    }).join("")
    : '<div class="empty-state"><strong>Nothing here.</strong><p>No sources match this diagnostic view.</p></div>';
}

async function loadCoverage() {
  try {
    state.coverage = await api("/api/coverage");
    renderCoverage();
  } catch {
    $("#coverage-list").innerHTML = '<div class="empty-state"><strong>Source health is unavailable.</strong><p>The diagnostics service did not respond.</p></div>';
  }
}

function renderProgress(data) {
  const progress = data.progress || {};
  const node = $("#sync-progress");
  node.hidden = !data.running;
  if (!data.running) return;
  $("#progress-stage").textContent = progress.stage || "Refreshing sources";
  $("#progress-label").textContent = `${formatNumber(progress.completed)} / ${formatNumber(progress.total)}`;
  $("#progress-bar").max = Math.max(1, progress.total || 1);
  $("#progress-bar").value = progress.completed || 0;
  $("#progress-current").textContent = progress.current || "";
}

async function refreshHealth() {
  clearTimeout(state.healthTimer);
  try {
    const data = await api("/api/health");
    const last = data.data?.last_run?.finished_at || data.data?.last_success_at;
    const age = last ? Date.now() - new Date(last).getTime() : Infinity;
    const node = $("#freshness");
    if (data.read_only) {
      $$(".admin-actions").forEach(actions => { actions.hidden = true; });
    }
    node.className = `freshness ${data.running || age < 36 * 3600000 ? "fresh" : age < 7 * 86400000 ? "stale" : "failed"}`;
    node.lastElementChild.textContent = data.running
      ? "Refreshing sources…"
      : data.read_only && last
        ? `Snapshot updated ${relative(last)}`
      : last
        ? `Updated ${relative(last)}${data.data.failing_sources ? ` · ${data.data.failing_sources} issue${data.data.failing_sources === 1 ? "" : "s"}` : ""}`
        : "No completed refresh";
    renderProgress(data);
    if (state.wasRunning && !data.running) {
      await Promise.all([loadJobs(), loadStats(), loadCoverage()]);
      toast("Source refresh complete");
    }
    state.wasRunning = data.running;
    state.healthTimer = setTimeout(refreshHealth, data.running ? 1800 : 30000);
  } catch {
    const node = $("#freshness");
    node.className = "freshness failed";
    node.lastElementChild.textContent = "Index unavailable";
    state.healthTimer = setTimeout(refreshHealth, 10000);
  }
}

async function startSync(path) {
  const buttons = [$("#sync-button"), $("#discover-button")];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const data = await api(path, { method: "POST" });
    if (!data.started && data.running) toast("A refresh is already running");
    else toast(path.endsWith("discover") ? "Employer discovery started" : "Source refresh started");
    state.wasRunning = Boolean(data.running);
    renderProgress(data);
    refreshHealth();
  } catch {
    toast("Could not start the refresh");
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

function clearFilters() {
  $("#search").value = "";
  $("#category").value = "";
  $("#company").value = "";
  $("#location").value = "";
  $("#remote").checked = false;
  $("#posted-within").value = "0";
  $("#target").value = "default";
  $("#trust").value = "verified";
  $("#sort").value = "newest";
  state.page = 1;
  loadFacets();
  loadJobs();
}

const initial = new URLSearchParams(location.search);
for (const id of ["search", "category", "location", "target", "trust", "sort", "posted-within"]) {
  const key = id === "search" ? "q" : id.replace("-", "_");
  if (initial.has(key) && $(`#${id}`)) $(`#${id}`).value = initial.get(key);
}
$("#remote").checked = initial.get("remote") === "true";
state.page = Math.max(1, Number(initial.get("page") || 1));

let searchTimer;
for (const selector of ["#search", "#location"]) {
  $(selector).addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.page = 1; loadJobs(); }, 260);
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
$("#density-toggle").addEventListener("click", () => {
  const compact = $("#job-grid").classList.toggle("compact");
  $("#density-toggle").setAttribute("aria-pressed", String(compact));
  $("#density-toggle").textContent = compact ? "Card view" : "Compact view";
});
$("#clear-filters").addEventListener("click", clearFilters);
$("#prev").addEventListener("click", () => { state.page -= 1; loadJobs(); $("#results").scrollIntoView(); });
$("#next").addEventListener("click", () => { state.page += 1; loadJobs(); $("#results").scrollIntoView(); });
$("#drawer-close").addEventListener("click", () => $("#drawer").close());
$("#drawer").addEventListener("click", event => { if (event.target === $("#drawer")) $("#drawer").close(); });
$("#drawer").addEventListener("change", event => {
  if (event.target.matches("#tracking-status")) setTracking(event.target.dataset.family, event.target.value);
});
$("#coverage-filter").addEventListener("change", renderCoverage);
$("#sync-button").addEventListener("click", () => startSync("/api/sync"));
$("#discover-button").addEventListener("click", () => startSync("/api/discover"));

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
