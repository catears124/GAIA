const state = { page: 1, total: 0, items: [], controller: null, coverage: null };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const number = (value) => new Intl.NumberFormat().format(Number(value || 0));

function relative(value, precision = "timestamp") {
  if (!value) return "Unknown";
  const delta = Date.now() - new Date(value).getTime();
  const minutes = Math.max(0, Math.round(delta / 60000));
  if (precision === "day") return `~${Math.max(1, Math.round(minutes / 1440))}d ago`;
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

function queryString() {
  const params = new URLSearchParams({
    page: state.page,
    page_size: $("#page-size").value,
    q: $("#search").value.trim(),
    category: $("#category").value,
    target: $("#target").value,
  });
  return params.toString();
}

async function loadJobs() {
  state.controller?.abort();
  state.controller = new AbortController();
  $("#jobs-body").innerHTML = '<tr><td colspan="8" class="empty">Loading role families…</td></tr>';
  try {
    const data = await api(`/api/families?${queryString()}`, { signal: state.controller.signal });
    state.items = data.items;
    state.total = data.total;
    renderJobs();
  } catch (error) {
    if (error.name !== "AbortError") {
      $("#jobs-body").innerHTML = `<tr><td colspan="8" class="empty error">${esc(error.message)}</td></tr>`;
    }
  }
}

function renderJobs() {
  const saved = new Set(JSON.parse(localStorage.getItem("gaia:saved") || "[]"));
  if (!state.items.length) {
    $("#jobs-body").innerHTML = '<tr><td colspan="8" class="empty">No role families match these filters.</td></tr>';
  } else {
    $("#jobs-body").innerHTML = state.items.map(item => {
      const locationPreview = item.locations.slice(0, 2).join(" · ");
      const extraLocations = Math.max(0, item.locations.length - 2);
      const sourceBadge = item.direct_openings ? "direct" : "fallback";
      return `<tr data-key="${esc(item.family_key)}">
        <td class="save-col"><button class="star ${saved.has(item.family_key) ? "saved" : ""}" data-save="${esc(item.family_key)}">☆</button></td>
        <td><strong title="${esc(exact(item.latest_posted_at))}">${esc(relative(item.latest_posted_at, item.posted_precision))}</strong><small>${item.latest_posted_at ? "employer supplied" : "date unavailable"}</small></td>
        <td><strong>${esc(item.company)}</strong><small>${esc(sourceBadge)}</small></td>
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
  const saved = new Set(JSON.parse(localStorage.getItem("gaia:saved") || "[]"));
  saved.has(key) ? saved.delete(key) : saved.add(key);
  localStorage.setItem("gaia:saved", JSON.stringify([...saved]));
  renderJobs();
}

async function openFamily(key) {
  const item = await api(`/api/families/${encodeURIComponent(key)}`);
  const openings = item.openings.map((opening, index) => {
    const locations = opening.location?.join(" · ") || "Location not stated";
    return `<article class="opening"><div><strong>Opening ${index + 1}</strong><p>${esc(locations)}</p><small>${esc(opening.source)} · ${esc(opening.source_mode)}</small></div><a href="${esc(opening.apply_url)}" target="_blank" rel="noopener">Apply ↗</a></article>`;
  }).join("");
  $("#drawer-content").innerHTML = `<p class="eyebrow">${esc(item.company)}</p><h2>${esc(item.title)}</h2><div class="drawer-meta"><span>${number(item.opening_count)} openings</span><span>${number(item.location_count)} locations</span><span>${esc(item.category)}</span></div><dl><dt>Employer posted</dt><dd>${esc(relative(item.latest_posted_at, item.posted_precision))}</dd><dt>First detected</dt><dd>${esc(relative(item.first_detected_at))}</dd><dt>Last verified</dt><dd>${esc(relative(item.last_verified_at))}</dd></dl><h3>Applications</h3>${openings}`;
  $("#drawer").showModal();
}

async function loadCoverage() {
  const data = await api("/api/coverage");
  state.coverage = data;
  const summary = data.summary || {};
  $("#metric-families").textContent = number(summary.families);
  $("#metric-companies").textContent = number(summary.companies);
  $("#metric-direct").textContent = summary.families ? `${Math.round(100 * Number(summary.direct_families || 0) / Number(summary.families))}%` : "—";
  $("#metric-health").textContent = `${number(data.healthy)}/${number(data.configured)}`;
  $("#coverage-summary").innerHTML = [
    ["Known target families", summary.families || 0, "union after conservative grouping"],
    ["Direct families", summary.direct_families || 0, "recovered from employer-controlled sources"],
    ["Backstop only", summary.backstop_only || 0, "known gap requiring a direct adapter"],
    ["Complete sources", data.healthy || 0, "finished their declared enumeration contract"],
  ].map(([label, value, note]) => `<article><span>${esc(label)}</span><strong>${number(value)}</strong><small>${esc(note)}</small></article>`).join("");
  $("#coverage-body").innerHTML = (data.sources || []).map(source => `<tr><td><strong>${esc(source.source)}</strong></td><td>${esc(source.mode)}</td><td><span class="status ${source.complete ? "ok" : "warn"}">${source.complete ? "complete" : "non-complete"}</span></td><td>${number(source.rows_scanned)}</td><td>${number(source.target_rows)}</td><td>${source.expected_rows == null ? "—" : number(source.expected_rows)}</td><td>${esc(relative(source.last_success_at))}</td><td title="${esc(source.last_error || "")}">${esc((source.last_error || "—").slice(0, 90))}</td></tr>`).join("");
}

async function refreshHealth() {
  try {
    const data = await api("/api/health");
    const pill = $("#health-pill");
    pill.textContent = data.running ? "syncing" : "live";
    pill.className = `pill ${data.running ? "busy" : "ok"}`;
    $("#sync-button").disabled = Boolean(data.running);
  } catch {
    $("#health-pill").textContent = "offline";
    $("#health-pill").className = "pill error";
  }
}

let searchTimer;
$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.page = 1; loadJobs(); }, 180); });
for (const id of ["#category", "#target", "#page-size"]) $(id).addEventListener("change", () => { state.page = 1; loadJobs(); });
$("#prev").addEventListener("click", () => { state.page--; loadJobs(); });
$("#next").addEventListener("click", () => { state.page++; loadJobs(); });
$("#jobs-body").addEventListener("click", event => {
  const save = event.target.closest("[data-save]");
  const open = event.target.closest("[data-open]");
  if (save) toggleSaved(save.dataset.save);
  if (open) openFamily(open.dataset.open);
});
$("#drawer-close").addEventListener("click", () => $("#drawer").close());
$("#sync-button").addEventListener("click", async () => { await api("/api/sync", { method: "POST" }); refreshHealth(); });
for (const tab of document.querySelectorAll(".tab")) tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === tab));
  const coverage = tab.dataset.view === "coverage";
  $("#jobs-view").hidden = coverage;
  $("#coverage-view").hidden = !coverage;
  if (coverage) loadCoverage();
});

document.addEventListener("keydown", event => {
  if (event.key === "/" && document.activeElement !== $("#search")) { event.preventDefault(); $("#search").focus(); }
  if (event.key === "Escape" && $("#drawer").open) $("#drawer").close();
});

loadJobs();
loadCoverage();
refreshHealth();
setInterval(refreshHealth, 5000);
