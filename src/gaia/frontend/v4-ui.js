"use strict";

// GAIA v4 treats market freshness and employer verification as orthogonal axes.
// This file overrides the legacy presentation while the rest of the product shell
// remains stable during the branch rollout.

function v4Activity(item) {
  return item.market_event_at || item.latest_posted_at || item.latest_sensor_reported_at || item.market_first_seen_at || item.first_detected_at || null;
}

function v4TimingLabel(item) {
  const value = v4Activity(item);
  if (!value) return { primary: "Date unavailable", secondary: "", value: null };
  const precision = item.market_event_precision || item.posted_precision || "timestamp";
  const kind = item.market_event_kind || (item.latest_posted_at ? "employer-posted" : item.latest_sensor_reported_at ? "sensor-reported" : "first-seen");
  const primary = kind === "employer-posted"
    ? `Posted ${relative(value, precision)}`
    : kind === "sensor-reported"
      ? `Reported ${relative(value, precision)}`
      : `Found ${relative(value, precision)}`;
  const detected = item.market_first_seen_at || item.first_detected_at;
  let secondary = "";
  if (item.verified && item.last_verified_at) {
    secondary = `Checked ${relative(item.last_verified_at)}`;
  } else if (detected && detected !== value) {
    secondary = `GAIA saw ${relative(detected)}`;
  }
  return { primary, secondary, value };
}

jobRow = function jobRowV4(item) {
  const saved = savedSet().has(item.family_key);
  const tracked = trackingMap()[item.family_key];
  const locations = item.locations || [];
  const location = locations.slice(0, 2).join(" · ") || "Location not stated";
  const timing = v4TimingLabel(item);
  const cycle = item.year ? (item.season ? `${item.season} ${item.year}` : String(item.year)) : "Cycle not stated";
  const evidence = Number(item.evidence_count || 0);
  return `<article class="job-row" role="listitem" data-key="${esc(item.family_key)}">
    <div class="job-date"><strong title="${esc(exact(timing.value))}">${esc(timing.primary)}</strong>${timing.secondary ? `<span>${esc(timing.secondary)}</span>` : ""}</div>
    <div class="job-role"><button data-open="${esc(item.family_key)}">${esc(item.title || "Untitled internship")}</button><span>${esc(item.company || "Unknown company")} · ${esc(cycle)}</span></div>
    <div class="job-location" title="${esc(locations.join(" · "))}">${esc(location)}${locations.length > 2 ? ` +${locations.length - 2}` : ""}</div>
    <div class="job-source">${sourceBadge(item)}<span class="tag">${esc(item.category || "technical")}</span><span class="tag">${formatNumber(item.opening_count)} opening${item.opening_count === 1 ? "" : "s"}</span>${evidence > 1 ? `<span class="tag">${formatNumber(evidence)} signals</span>` : ""}${tracked ? `<span class="tag">${esc(tracked)}</span>` : ""}</div>
    <div class="row-actions"><button class="icon-button ${saved ? "saved" : ""}" data-save="${esc(item.family_key)}" aria-label="${saved ? "Remove saved job" : "Save job"}">${saved ? "♥" : "♡"}</button><button class="open-button" data-open="${esc(item.family_key)}">Open</button></div>
  </article>`;
};

openFamily = async function openFamilyV4(key) {
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
      const direct = !["registry", "market-sensor", "external-index", "verification-lead", "lead"].includes(opening.source_mode);
      const timing = opening.posted_at
        ? `Employer posted ${exact(opening.posted_at)}`
        : opening.sensor_reported_at
          ? `Market source reported ${exact(opening.sensor_reported_at)}`
          : `GAIA first detected ${exact(opening.first_detected_at)}`;
      const corroboration = Number(opening.evidence_count || 0) > 1 ? ` · ${formatNumber(opening.evidence_count)} independent signals` : "";
      return `<article class="opening"><div class="opening-head"><div><strong>Opening ${index + 1}</strong><p>${esc((opening.location || []).join(" · ") || "Location not stated")}</p><small>${esc(timing)} · ${direct ? "Direct employer application" : "Unverified market lead"}${esc(corroboration)}</small></div><a class="apply-link" href="${esc(safeUrl(opening.apply_url))}" target="_blank" rel="noopener noreferrer">View and apply →</a></div></article>`;
    }).join("");
    const marketTiming = v4TimingLabel(item);
    const trustNote = item.verified
      ? "GAIA independently recovered this application from an employer-controlled hiring surface."
      : "This role is visible because a market source detected it. GAIA has not independently verified the employer application yet.";
    $("#drawer-content").innerHTML = `<div class="drawer-body">
      <span class="badge ${item.verified ? "verified" : "lead"}">${item.verified ? "Employer verified" : "Market lead"}</span>
      <h2 id="drawer-title">${esc(item.title)}</h2>
      <div class="drawer-meta"><span class="tag">${esc(item.company)}</span><span class="tag">${esc(item.category)}</span><span class="tag">${formatNumber(item.opening_count)} opening${item.opening_count === 1 ? "" : "s"}</span>${Number(item.evidence_count || 0) > 1 ? `<span class="tag">${formatNumber(item.evidence_count)} signals</span>` : ""}</div>
      <div class="trust-note ${item.verified ? "" : "lead"}">${esc(trustNote)}</div>
      <div class="fact-grid"><div><span>Latest market event</span><strong>${esc(marketTiming.primary)}</strong></div><div><span>Employer posted</span><strong>${item.latest_posted_at ? esc(exact(item.latest_posted_at)) : "Not published / not verified"}</strong></div><div><span>Last employer check</span><strong>${item.last_verified_at ? esc(exact(item.last_verified_at)) : "Not yet verified"}</strong></div></div>
      <label class="tracking-field"><span>Application status</span><select id="tracking-status" data-family="${esc(key)}">${options}</select></label>
      <h3>Applications</h3>${openings || "<p>No application URL is currently available.</p>"}
    </div>`;
  } catch {
    $("#drawer-content").innerHTML = '<div class="drawer-body"><h2>Could not load this role.</h2><p>Close the panel and try again.</p></div>';
  }
};

refreshHealth = async function refreshHealthV4() {
  clearTimeout(state.healthTimer);
  try {
    const data = await api("/api/health");
    const inventory = data.inventory || {};
    const market = data.market || {};
    const fresh = Number(inventory.fresh || 0);
    const total = Number(inventory.total || 0);
    const unhealthy = Number(inventory.unhealthy || 0);
    const activity = inventory.latest_activity_at || null;
    const node = $("#freshness");
    node.className = `freshness ${data.ok ? "fresh" : unhealthy ? "stale" : "fresh"}`;
    if (inventory.kind === "market-sensors") {
      const recent = Number(market.new_verified_24h || 0);
      node.lastElementChild.textContent = `${formatNumber(fresh)} / ${formatNumber(total)} market sensors current · ${formatNumber(recent)} new verified / 24h`;
    } else {
      node.lastElementChild.textContent = data.running
        ? `${formatNumber(fresh)} / ${formatNumber(total)} sources current · crawling`
        : data.ok
          ? `${formatNumber(total)} sources current`
          : `${formatNumber(fresh)} / ${formatNumber(total)} sources current`;
    }
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
};

// app-v2 starts its first requests before this compatibility layer is evaluated.
// Re-render immediately so an already-returned legacy-shaped response cannot remain
// on screen after the v4 functions have replaced the presenters.
clearTimeout(state.healthTimer);
Promise.all([loadJobs(), loadStats(), refreshHealth()]);
