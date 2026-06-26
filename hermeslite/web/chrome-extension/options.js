(() => {
  "use strict";

  const $url = document.getElementById("serverUrl");
  const $token = document.getElementById("token");
  const $status = document.getElementById("status");

  // Load saved settings.
  chrome.storage.local.get(["serverUrl", "token"], (cfg) => {
    $url.value = cfg.serverUrl || "";
    $token.value = cfg.token || "";
  });

  function setStatus(msg, ok) {
    $status.textContent = msg;
    $status.className = "status " + (ok ? "ok" : "err");
  }

  // Test connection.
  document.getElementById("btn-test").addEventListener("click", async () => {
    const base = $url.value.trim().replace(/\/+$/, "");
    if (!base) { setStatus("Enter a server URL first.", false); return; }
    setStatus("Connecting…", null);
    try {
      const headers = {};
      const tok = $token.value.trim();
      if (tok) headers["Authorization"] = "Bearer " + tok;
      const res = await fetch(base + "/api/status", { headers });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      setStatus("Connected — v" + (data.version || "?") + ", model: " + (data.model || "?"), true);
    } catch (err) {
      setStatus("Connection failed: " + err.message, false);
    }
  });

  // Save settings.
  document.getElementById("btn-save").addEventListener("click", () => {
    const base = $url.value.trim().replace(/\/+$/, "");
    const tok = $token.value.trim();
    chrome.storage.local.set({ serverUrl: base, token: tok }, () => {
      setStatus("Settings saved.", true);
    });
  });
})();
