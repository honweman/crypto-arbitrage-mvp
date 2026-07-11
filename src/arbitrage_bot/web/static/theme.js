(() => {
  const STORAGE_KEY = "cryptoArbTheme";
  const THEMES = new Set(["light", "dark"]);
  const root = document.documentElement;
  const systemDark = window.matchMedia?.("(prefers-color-scheme: dark)") || null;

  function storedTheme() {
    try {
      const value = localStorage.getItem(STORAGE_KEY);
      return THEMES.has(value) ? value : null;
    } catch {
      return null;
    }
  }

  function effectiveTheme() {
    if (THEMES.has(root.dataset.theme)) return root.dataset.theme;
    const stored = storedTheme();
    if (stored) return stored;
    return systemDark?.matches ? "dark" : "light";
  }

  function syncToggle() {
    const toggle = document.getElementById("theme-toggle");
    if (!toggle) return;
    const dark = effectiveTheme() === "dark";
    toggle.checked = dark;
    toggle.setAttribute("aria-checked", dark ? "true" : "false");
  }

  function setTheme(theme) {
    if (!THEMES.has(theme)) return;
    root.dataset.theme = theme;
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // The selected theme still applies for this page when storage is unavailable.
    }
    syncToggle();
    window.dispatchEvent(new CustomEvent("crypto-arb-theme-change", {
      detail: { theme },
    }));
  }

  const initial = storedTheme();
  if (initial) root.dataset.theme = initial;

  function setupToggle() {
    const toggle = document.getElementById("theme-toggle");
    if (!toggle) return;
    syncToggle();
    toggle.addEventListener("change", () => {
      setTheme(toggle.checked ? "dark" : "light");
    });
  }

  function syncSystemTheme() {
    if (storedTheme()) return;
    root.removeAttribute("data-theme");
    syncToggle();
    window.dispatchEvent(new CustomEvent("crypto-arb-theme-change", {
      detail: { theme: effectiveTheme() },
    }));
  }

  if (systemDark?.addEventListener) {
    systemDark.addEventListener("change", syncSystemTheme);
  } else if (systemDark?.addListener) {
    systemDark.addListener(syncSystemTheme);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setupToggle);
  } else {
    setupToggle();
  }

  window.CryptoArbTheme = {
    setTheme,
    get theme() {
      return effectiveTheme();
    },
  };
})();
