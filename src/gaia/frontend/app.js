const state = {
  page: 1,
  total: 0,
  items: [],
  controller: null,
  coverage: null,
  wasRunning: false,
};
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[char]));
const number = value => new Intl.NumberFormat().format(Number(value || 0));

function relative(value, precision = "timestamp") {
  if (!value) return "Unknown";
  const delta = Date.now() - new Date(value).getTime();
  const minutes = Math.max(0, Math.round(delta / 60000));
  if (precision === "day") {
    const days = Math.max(1, Math.round(minutes / 1440));
    return `~${days} ${days === 1 ? "day" : "days"} ago`;
  }
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function exact(value) {
  return value ? new Date(value).toLocaleString() : "Employer did not expose a publication date";
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function savedSet() {
  return new Set(JSON.parse(localStorage.getItem("gaia:saved") || "[]"));
}

function trackingMap() {
  return JSON.parse(localStorage.getItem("gaia:tracking") || "{}");
}

function setTracking(key, value) {
  const tracking = trackingMap();
  if (value) tracking[key] = value;
  else delete tracking[key];
  localStorage.setItem("gaia:tracking", JSON.stringify(tracking));
  renderJobs();
}

function queryString() {
  return new URLSearchParams({
    page: state.page,
    page_size: $("#page-size").value,
    q: $("#search").value.trim(),
    category: $("#category").value,
    target: $("#target").value,
    trust: $("#trust").value,
  }).toString();
}

async function loadStats() {
  try {
    const data = await api("/api/stats");
    $("#metric-active").textContent = number(data.active_listings);
    $("#metric-new").textContent = number(data.new_24h);
    $("#metric-companies").textContent = number(data.companies);
    $("#metric-leads").textContent = number(data.leads);
  } catch {
    for (const id of ["#metric-active", "#metric-new", "#metric-companies", "#metric-leads"]) {
      $(id).textContent = "—";
    }
  }
}

async function loadJobs() {
  state.controller?.abort();
  state.controller = new AbortController();
  const trust = $("#trust").value;
  const label = trust === "leads" ? "Loading unresolved leads…" : "Loading verified internships…";
  $("#jobs-body").innerHTML = `<tr><td colspan="8" class="empty">${label}</td></tr>`;
  try {
    const data = await api(`/api/families?${queryString()}`, {
      signal: state.controller.signal,
    });
    state.items = data.items;
    state.total = data.total;
    renderJobs();
  } catch (error) {
    if (error.name !== "AbortError") {
      $("#jobs-body").innerHTML = `<tr><td colspan="8" class="empty error">${esc(error.message)}</td></tr>`;
    }
  }
}

function dateCell(item) {
  const verified = relative(item.last_verified_at || item.first_detected_at);
  if (item.latest_posted_at) {
    return `<strong title="${esc(exact(item.latest_posted_at))}">${esc(relative(item.latest_posted_at, item.posted_precision))}</strong><small class="date-detected">verified ${esc(verified)}</small>`;
  }
  if (item.verified) {
    return `<span class="date-unavailable">Posted date unavailable</span><small class="date-detected">verified ${esc(verified)}</small>`;
  }
  return `<span class="date-unavailable">Unverified lead</span><small class="date-detected">detected ${esc(relative(item.first_detected_at))}</small>`;
}

function qualityBadge(item) {
  if (item.verified) return '<span class="quality verified">verified</span>';
  return '<span class="quality lead">lead</span>';
}

function renderJobs() {
  const saved = savedSet();
  const tracking = trackingMap();
  if (!state.items.length) {
    const trust = $("#trust").value;
    const message = trust === "verified"
      ? "No verified internships match these filters. Try the lead queue or run Discover companies."
      : "No leads match these filters.";
    $("#jobs-body").innerHTML = `<tr><td colspan="8" class="empty">${message}</td></tr>`;
  } else {
    $("#jobs-body").innerHTML = state.items.map(item => {
      const locationPreview = item.locations.slice(0, 2).join(" · ");
      const extraLocations = Math.max(0, item.locations.length - 2);
      const status = tracking[item.family_key] || "";
      return `<tr data-key="${esc(item.family_key)}" class="${item.verified ? "row-verified" : "row-lead"}">
        <td class="save-col"><button class="star ${saved.has(item.family_key) ? "saved" : ""}" data-save="${esc(item.family_key)}" aria-label="Save role">☆</button></td>
        <td>${dateCell(item)}</td>
        <td><strong>${esc(item.company)}</strong><small>${qualityBadge(item)}${status ? ` · ${esc(status)}` : ""}</small></td>
        <td><button class="role-link" data-open="${esc(item.family_key)}">${esc(item.title)}</button><small>${esc(item.target_match.replaceAll("_", " "))}</small></td>
        <td><strong>${number(item.opening_count)}</strong><small>${item.opening_count === 1 ? "application" : "applications"}</small></td>
        <td>${esc(locationPreview || "Not stated")}${extraLocations ? `<small>+${extraLocations} more</small>` : ""}</td>
        <td><span class="tag">${esc(item.category)}</span></td>
        <td><button class="expand" data-open="${esc(item.family_key)}">View</button></td>
      </tr>`;
    }).join("");
  }
  const size = Number($("#page-size").value);
  const start = state.total ? (state.page - 1) * size + 1 : 0;
  const end = Math.min(state.total, state.page * size);
  $("#page-label").textContent = `${number(start)}–${number(end)} of ${number(state.total)} families`;
  $("#prev").disabled = state.page <= 1;
  $("#next").disabled = end >= state.total;
}

function toggleSaved(key) {
  const saved = savedSet();
  saved.has(key) ? saved.delete(key) : saved.add(key);
  localStorage.setItem("gaia:saved", JSON.stringify([...saved]));
  renderJobs();
}

async function openFamily(key) {
  const item = await api(`/api/families/${encodeURIComponent(key)}`);
  const currentStatus = trackingMap()[key] || "";
  const options = ["", "applied", "interview", "offer", "rejected", "ignored"]
    .map(value => `<option value="${value}" ${value === currentStatus ? "selected" : ""}>${value || "Not tracked"}</option>`)
    .join("");
  const openings = item.openings.map((opening, index) => {
    const locations = opening.location?.join(" · ") || "Location not stated";
    const variants = (opening.source_variants || []).join(" · ");
    const sourceMode = opening.source_mode === "registry" || opening.source_mode === "external-index"
      ? "lead source"
      : "employer source";
    return `<article class="opening ${opening.source_mode === "registry" || opening.source_mode === "external-index" ? "lead" : "verified"}"><div><strong>Opening ${index + 1}</strong><p>${esc(locations)}</p><small>${esc(sourceMode)} · ${esc(opening.source)}${variants ? ` · ${esc(variants)}` : ""}</small></div><a href="${esc(opening.apply_url)}" target="_blank" rel="noopener">Apply ↗</a></article>`;
  }).join("");
  $("#drawer-content").innerHTML = `<p class="eyebrow">${esc(item.company)} · ${item.verified ? "verified" : "lead"}</p><h2>${esc(item.title)}</h2><div class="drawer-meta"><span>${number(item.opening_count)} openings</span><span>${number(item.location_count)} locations</span><span>${esc(item.category)}</span></div><dl><dt>Employer posted</dt><dd title="${esc(exact(item.latest_posted_at))}">${item.latest_posted_at ? esc(relative(item.latest_posted_at, item.posted_precision)) : "Unavailable from employer"}</dd><dt>First detected</dt><dd>${esc(relative(item.first_detected_at))}</dd><dt>Last verified</dt><dd>${esc(relative(item.last_verified_at))}</dd><dt>Tracking</dt><dd><select id="tracking-status" data-family="${esc(key)}">${options}</select></dd></dl><h3>Applications</h3>${openings}`;
  $("#drawer").showModal();
}

function isActionable(source) {
  if (source.last_error || ["broken", "truncated"].includes(source.status)) return true;
  return source.status === "empty" && ["board", "domain"].includes(source.mode);
}

function sourceState(source) {
  const mapping = {
    ok: ["complete", "ok"], loaded: ["loaded", "ok"], verified: ["verified", "ok"],
    indexed: ["external index", "warn"], blocked: ["access limited", "blocked"],
    stale: ["closed/stale", "stale"], dormant: ["dormant watch", "dormant"],
    unstructured: ["no structured data", "muted"], partial: ["partial", "warn"],
    mixed: ["mixed", "warn"], truncated: ["truncated", "error"],
    empty: source.mode === "board-search" ? ["no internships", "muted"] : ["suspiciously empty", "error"],
    broken: ["broken", "error"],
  };
  return mapping[source.status] || [source.status || "unknown", source.complete ? "ok" : "warn"];
}

function sourceMatchesFilter(source, filter) {
  const note = source.note || "";
  if (filter === "all") return true;
  if (filter === "actionable") return source.scope === "current" && isActionable(source);
  if (filter === "access") return source.scope === "current" && (source.status === "blocked" || note.includes("access-blocked"));
  if (filter === "stale") return source.scope === "current" && source.mode === "verification" && (source.status === "stale" || note.includes("stale/closed"));
  if (filter === "historical") return source.scope === "historical";
  return true;
}

function renderCoverageSources() {
  const filter = $("#coverage-filter").value;
  const sources = [...(state.coverage?.sources || [])]
    .filter(source => sourceMatchesFilter(source, filter))
    .sort((left, right) => Number(isActionable(right)) - Number(isActionable(left)) || left.source.localeCompare(right.source));
  $("#coverage-source-count").textContent = `${number(sources.length)} sources`;
  if (!sources.length) {
    $("#coverage-body").innerHTML = '<tr><td colspan="9" class="empty">No sources in this queue.</td></tr>';
    return;
  }
  $("#coverage-body").innerHTML = sources.map(source => {
    const [label, tone] = sourceState(source);
    const detail = source.last_error || source.note || "—";
    return `<tr><td><strong>${esc(source.source)}</strong></td><td>${esc(source.scope || "current")}</td><td>${esc(source.mode)}</td><td><span class="status ${tone}">${esc(label)}</span></td><td>${number(source.rows_scanned)}</td><td>${number(source.target_rows)}</td><td>${source.expected_rows == null ? "—" : number(source.expected_rows)}</td><td title="${esc(exact(source.last_attempt_at))}">${esc(relative(source.last_attempt_at))}</td><td class="source-detail" title="${esc(detail)}">${esc(detail.slice(0, 150))}</td></tr>`;
  }).join("");
}

async function loadCoverage() {
  const data = await api("/api/coverage");
  state.coverage = data;
  const summary = data.summary || {};
  const contract = data.contract || {};
  const recall = summary.registry_recall_percent;
  const actionable = Number(contract.actionable_anomalies || 0);
  const unresolved = Number(summary.registry_only || 0);
  const trustworthy = unresolved === 0 && actionable === 0 && recall != null;
  $("#coverage-grade").textContent = trustworthy ? "benchmark closed" : "known gaps";
  $("#coverage-grade").className = `status ${trustworthy ? "ok" : "warn"}`;
  $("#coverage-contract").classList.toggle("incomplete", !trustworthy);
  $("#coverage-contract-text").textContent = recall == null
    ? "No target benchmark is loaded yet, so GAIA cannot make a recall statement."
    : `Verified feed is employer-recovered. Public indexes report ${number(summary.registry_floor)} benchmark apps; ${number(summary.registry_only)} are still leads. ${number(actionable)} current sources need engineering.`;
  $("#coverage-summary").innerHTML = [
    ["Verified applications", summary.direct_applications || 0, "employer-controlled source recovered"],
    ["Lead applications", summary.registry_only || 0, "index rows awaiting verification"],
    ["Benchmark recall", recall == null ? "—" : `${recall}%`, "independent recovery of public-index apps"],
    ["Direct-only", summary.direct_only || 0, "found before or outside public indexes"],
    ["Actionable gaps", actionable, "actual current crawler failures"],
    ["Source universe", contract.configured_sources || 0, "latest run source records"],
  ].map(([label, value, note]) => `<article><span>${esc(label)}</span><strong>${typeof value === "number" ? number(value) : esc(value)}</strong><small>${esc(note)}</small></article>`).join("");
  renderCoverageSources();
}

function renderProgress(data) {
  const progress = data.progress || {};
  const box = $("#sync-progress");
  box.hidden = !data.running;
  if (!data.running) return;
  const total = Number(progress.total || 0);
  const completed = Number(progress.completed || 0);
  $("#progress-stage").textContent = progress.stage || "Working";
  $("#progress-label").textContent = total ? `${number(completed)} / ${number(total)}` : `${Math.round(progress.elapsed_seconds || 0)}s`;
  $("#progress-bar").max = Math.max(1, total);
  $("#progress-bar").value = total ? completed : 0;
  $("#progress-current").textContent = progress.current || `${Math.round(progress.elapsed_seconds || 0)} seconds elapsed`;
}

async function refreshHealth() {
  try {
    const data = await api("/api/health");
    const pill = $("#health-pill");
    pill.textContent = data.running ? (data.progress?.mode === "discover" ? "discovering" : "refreshing") : "live";
    pill.className = `pill ${data.running ? "busy" : "ok"}`;
    $("#sync-button").disabled = Boolean(data.running);
    $("#discover-button").disabled = Boolean(data.running);
    renderProgress(data);
    if (state.wasRunning && !data.running) {
      await Promise.all([loadJobs(), loadStats(), loadCoverage()]);
    }
    state.wasRunning = Boolean(data.running);
  } catch {
    $("#health-pill").textContent = "offline";
    $("#health-pill").className = "pill error";
  }
}

async function startSync(path) {
  await api(path, { method: "POST" });
  state.wasRunning = true;
  await refreshHealth();
}

let searchTimer;
$("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.page = 1; loadJobs(); }, 180);
});
for (const id of ["#category", "#target", "#trust", "#page-size"]) {
  $(id).addEventListener("change", () => { state.page = 1; loadJobs(); });
}
$("#coverage-filter").addEventListener("change", renderCoverageSources);
$("#prev").addEventListener("click", () => { state.page--; loadJobs(); });
$("#next").addEventListener("click", () => { state.page++; loadJobs(); });
$("#jobs-body").addEventListener("click", event => {
  const save = event.target.closest("[data-save]");
  const open = event.target.closest("[data-open]");
  if (save) toggleSaved(save.dataset.save);
  if (open) openFamily(open.dataset.open);
});
$("#drawer").addEventListener("change", event => {
  if (event.target.matches("#tracking-status")) setTracking(event.target.dataset.family, event.target.value);
});
$("#drawer-close").addEventListener("click", () => $("#drawer").close());
$("#sync-button").addEventListener("click", () => startSync("/api/sync"));
$("#discover-button").addEventListener("click", () => startSync("/api/discover"));
for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === tab));
    const coverage = tab.dataset.view === "coverage";
    $("#jobs-view").hidden = coverage;
    $("#coverage-view").hidden = !coverage;
    if (coverage) loadCoverage();
  });
}
document.addEventListener("keydown", event => {
  if (event.key === "/" && document.activeElement !== $("#search")) {
    event.preventDefault();
    $("#search").focus();
  }
  if (event.key === "Escape" && $("#drawer").open) $("#drawer").close();
});

Promise.all([loadJobs(), loadStats(), loadCoverage(), refreshHealth()]);
setInterval(refreshHealth, 1500);
