/* Shared theme handling for the VSL scan app.
   Include with:  <script src="/static/theme.js"></script>
   Dark is the default; preference persists in localStorage and falls back
   to the OS setting. Pages with canvas plots can listen for the
   "themechange" event to repaint. */

(function () {
  const KEY = "vsl-scan-theme";

  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }

  function preferred() {
    // ?theme=light|dark in the URL wins (and persists) — handy for links.
    const q = new URLSearchParams(window.location.search).get("theme");
    if (q === "light" || q === "dark") {
      try { localStorage.setItem(KEY, q); } catch (e) {}
      return q;
    }
    const s = stored();
    if (s === "light" || s === "dark") return s;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light" : "dark";
  }

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    document.querySelectorAll(".theme-toggle").forEach(function (b) {
      b.textContent = theme === "light" ? "☾" : "☀";
      b.title = theme === "light" ? "Switch to dark theme" : "Switch to light theme";
    });
    window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: theme } }));
  }

  window.toggleTheme = function () {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    try { localStorage.setItem(KEY, next); } catch (e) {}
    apply(next);
  };

  // Read a CSS custom property (used by canvas plots for theme-aware colors).
  window.cssVar = function (name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  };

  // Apply before first paint if possible; re-apply on DOM ready so toggle
  // buttons rendered later pick up the right glyph.
  apply(preferred());
  document.addEventListener("DOMContentLoaded", function () { apply(preferred()); });
})();
