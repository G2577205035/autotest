"use strict";

// Apply the saved palette before loading CSS, including on the sign-in screen.
(function () {
  const key = "liema.theme";
  const root = document.documentElement;
  function savedTheme() {
    try { return localStorage.getItem(key) === "dark" ? "dark" : "light"; }
    catch (_) { return "light"; }
  }
  function syncButtons() {
    const next = root.dataset.theme === "light" ? "深色" : "浅色";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.setAttribute("aria-label", "切换为" + next + "模式");
      button.title = "切换为" + next + "模式";
      button.querySelector("[data-theme-label]").textContent = next + "模式";
    });
  }
  function apply(theme, persist) {
    theme = theme === "light" ? "light" : "dark";
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    if (persist) {
      try { localStorage.setItem(key, theme); } catch (_) { /* Session-only fallback. */ }
    }
    syncButtons();
    window.dispatchEvent(new CustomEvent("liema:themechange", { detail: { theme: theme } }));
  }
  apply(savedTheme(), false);
  document.addEventListener("DOMContentLoaded", syncButtons);
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-theme-toggle]")) {
      apply(root.dataset.theme === "light" ? "dark" : "light", true);
    }
  });
  window.addEventListener("storage", function (event) {
    if (event.key === key || event.key === null) apply(savedTheme(), false);
  });
})();
