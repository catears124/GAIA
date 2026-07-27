"use strict";

// Product defaults: show the whole active technical-internship inventory. Confidence
// and cycle filters remain available, but they must not silently hide most of the market.
const liveParams = new URLSearchParams(location.search);
if (!liveParams.has("target")) $("#target").value = "";
if (!liveParams.has("trust")) $("#trust").value = "all";

const baseJobCard = jobCard;
jobCard = function liveJobCard(item) {
  const saved = savedSet().has(item.family_key);
  const status = trackingMap()[item.family_key];
  const locations = item.locations || [];
  const location = locations.slice(0, 2).join(" · ") || "Location not stated";
  const found = item.first_detected_at ? `Found ${relative(item.first_detected_at)}` : "First seen unknown";
  const employerPosted = item.latest_posted_at
    ? ` · Employer posted ${relative(item.latest_posted_at, item.posted_precision)}`
    : "";
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
      ${item.year ? `<span class="tag">${esc(item.season ? `${item.season} ${item.year}` : item.year)}</span>` : '<span class="tag">cycle unspecified</span>'}
      ${status ? `<span class="tag">${esc(status)}</span>` : ""}
    </div>
    <div class="card-footer">
      <p><strong title="${esc(exact(item.first_detected_at))}">${esc(found)}</strong><small>${esc(employerPosted)}</small><br>${esc(location)}${locations.length > 2 ? ` +${locations.length - 2}` : ""}</p>
      <button class="open-button" data-open="${esc(item.family_key)}">View role →</button>
    </div>
  </article>`;
};

// Replace snapshot-oriented health polling with source-level progress and refresh the
// visible feed whenever the underlying inventory advances.
refreshHealth = async function liveRefreshHealth() {
  clearTimeout(state.healthTimer);
  try {
    const data = await api("/api/health");
    const inventory = data.inventory || {};
    const node = $("#freshness");
    const fresh = Number(inventory.fresh || 0);
    const total = Number(inventory.total || 0);
    const overdue = Number(inventory.overdue || 0);
    const degraded = Number(inventory.degraded || 0);
    const never = Number(inventory.never_completed || 0);

    if (data.read_only) $$(".admin-actions").forEach(actions => { actions.hidden = true; });
    node.className = `freshness ${data.ok ? "fresh" : degraded ? "failed" : "stale"}`;
    node.lastElementChild.textContent = data.running
      ? `${formatNumber(fresh)} / ${formatNumber(total)} sources fresh · crawling`
      : data.ok
        ? `${formatNumber(total)} sources current`
        : `${formatNumber(fresh)} / ${formatNumber(total)} sources fresh · ${formatNumber(overdue + never)} catching up${degraded ? ` · ${formatNumber(degraded)} degraded` : ""}`;

    renderProgress(data);
    await Promise.all([loadJobs(), loadStats()]);
    if (state.view === "coverage") await loadCoverage();
    state.wasRunning = data.running;
    state.healthTimer = setTimeout(refreshHealth, data.running || !data.ok ? 10000 : 30000);
  } catch {
    const node = $("#freshness");
    node.className = "freshness failed";
    node.lastElementChild.textContent = "Index unavailable";
    state.healthTimer = setTimeout(refreshHealth, 10000);
  }
};

// The original deferred script already started one load with HTML defaults. Re-run once
// with live defaults, then polling above keeps the open page synchronized automatically.
state.page = 1;
Promise.all([loadFacets(), loadJobs(), loadStats(), refreshHealth()]);

$("#clear-filters").addEventListener("click", () => {
  setTimeout(() => {
    $("#target").value = "";
    $("#trust").value = "all";
    state.page = 1;
    Promise.all([loadFacets(), loadJobs(), loadStats()]);
  }, 0);
});
