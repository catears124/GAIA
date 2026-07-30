"use strict";

(() => {
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const formatNumber = value => new Intl.NumberFormat().format(Number(value || 0));

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
    const trigger = $("#trust") || $("#search");
    trigger?.dispatchEvent(new Event("change", { bubbles: true }));
    document.querySelector("#results")?.scrollIntoView({ behavior: "smooth" });
  }

  async function copySearch() {
    try {
      await navigator.clipboard.writeText(location.href);
      $("#copy-search").textContent = "Copied";
      setTimeout(() => { $("#copy-search").textContent = "Copy search"; }, 1400);
    } catch {
      prompt("Copy this search URL", location.href);
    }
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
    } catch {
      keys = [];
    }
    if (!keys.length) {
      alert("Save at least one role before exporting.");
      return;
    }
    const button = $("#export-saved");
    button.disabled = true;
    button.textContent = "Building export…";
    const rows = await Promise.all(keys.map(async key => {
      try {
        const response = await fetch(`/api/families/${encodeURIComponent(key)}?trust=all`, { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(String(response.status));
        const item = await response.json();
        const first = item.openings?.[0] || {};
        return [item.company, item.title, tracking[key] || "saved", first.apply_url || "", item.latest_posted_at || "", item.last_verified_at || ""];
      } catch {
        return ["", key, tracking[key] || "saved", "", "", ""];
      }
    }));
    const csv = [
      ["Company", "Role", "Status", "Application URL", "Employer posted", "Last checked"],
      ...rows,
    ].map(row => row.map(csvCell).join(",")).join("\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    link.download = `gaia-saved-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
    button.disabled = false;
    button.textContent = "Export CSV";
  }

  async function renderHonestHealth() {
    const node = $("#freshness");
    if (!node) return;
    try {
      const response = await fetch("/api/health", { headers: { Accept: "application/json" } });
      if (!response.ok) return;
      const data = await response.json();
      const inventory = data.inventory || {};
      const fresh = Number(inventory.fresh || 0);
      const total = Number(inventory.total || 0);
      const unhealthy = Number(inventory.unhealthy || 0);
      const running = Number(inventory.running || 0);
      const degraded = Number(inventory.degraded || 0);
      const percent = total ? (100 * fresh / total).toFixed(1) : "0.0";
      const label = node.querySelector("span:last-child");
      if (label) label.textContent = unhealthy
        ? `${percent}% current · ${formatNumber(unhealthy)} catching up${running ? ` · ${running} crawling` : ""}`
        : `${formatNumber(total)} sources current${running ? ` · ${running} crawling` : ""}`;
      node.title = `${formatNumber(fresh)} of ${formatNumber(total)} validated sources are within the freshness window. ${formatNumber(degraded)} have degraded latest results.`;
      node.classList.toggle("fresh", unhealthy === 0);
      node.classList.toggle("stale", unhealthy > 0);
    } catch {
      // app-v2 owns the unavailable state.
    }
  }

  document.addEventListener("click", event => {
    const preset = event.target.closest("[data-preset]");
    if (preset) applyPreset(preset.dataset.preset);
  });
  $("#copy-search")?.addEventListener("click", copySearch);
  $("#export-saved")?.addEventListener("click", exportSaved);
  renderHonestHealth();
  setInterval(renderHonestHealth, 10000);
})();
