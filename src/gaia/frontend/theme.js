"use strict";

(() => {
  const STORAGE_KEY = "gaia:theme";
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function storedTheme() {
    try {
      const value = localStorage.getItem(STORAGE_KEY);
      return value === "dark" || value === "light" ? value : null;
    } catch {
      return null;
    }
  }

  function preferredTheme() {
    return storedTheme() || (media.matches ? "dark" : "light");
  }

  function applyTheme(theme, persist = false) {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute(
      "content",
      theme === "dark" ? "#0d0f10" : "#111111",
    );

    const toggle = document.querySelector("#theme-toggle");
    if (toggle) {
      const isDark = theme === "dark";
      toggle.setAttribute("aria-pressed", String(isDark));
      toggle.setAttribute("aria-label", `Switch to ${isDark ? "light" : "dark"} mode`);
      toggle.title = `Switch to ${isDark ? "light" : "dark"} mode`;
      const label = toggle.querySelector(".theme-label");
      if (label) label.textContent = isDark ? "Light" : "Dark";
      const icon = toggle.querySelector(".theme-icon");
      if (icon) icon.textContent = isDark ? "☀" : "☾";
    }

    if (persist) {
      try { localStorage.setItem(STORAGE_KEY, theme); } catch { /* Storage is optional. */ }
    }
  }

  applyTheme(preferredTheme());

  window.addEventListener("DOMContentLoaded", () => {
    applyTheme(preferredTheme());
    document.querySelector("#theme-toggle")?.addEventListener("click", () => {
      const current = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
      applyTheme(current === "dark" ? "light" : "dark", true);
    });
  });

  media.addEventListener?.("change", event => {
    if (!storedTheme()) applyTheme(event.matches ? "dark" : "light");
  });
})();
