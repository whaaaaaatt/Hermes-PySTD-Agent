"""Build script: generate a Chrome extension from the existing web frontend.

Reads the existing ``static/app.js``, ``static/style.css``, and
``static/index.html``, applies minimal transformations, and outputs
a complete Chrome Manifest V3 extension under ``chrome-extension/``.

Usage::

    python hermeslite/web/build_chrome_ext.py

The output directory (``hermeslite/web/chrome-extension/``) and the
accompanying ``.zip`` are ephemeral build artifacts — add the directory
to ``.gitignore``.
"""
from __future__ import annotations

import json
import os
import struct
import shutil
import zlib
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_STATIC = _HERE / "static"
_OUT = _HERE / "chrome-extension"
_ZIP_PATH = _HERE / "chrome-extension.zip"

# Accent colour matching the dashboard (#b45309 → RGB).
_ICON_COLOR = (180, 83, 9)


# ---------------------------------------------------------------------------
# Minimal PNG generator (no third-party deps)
# ---------------------------------------------------------------------------

def _create_png(width: int, height: int, r: int, g: int, b: int) -> bytes:
    """Return a valid PNG file (RGBA, solid colour) as bytes."""
    sig = b"\x89PNG\r\n\x1a\n"

    # --- IHDR -----------------------------------------------------------
    ihdr_payload = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_payload) & 0xFFFFFFFF
    ihdr = (
        struct.pack(">I", 13) + b"IHDR" + ihdr_payload + struct.pack(">I", ihdr_crc)
    )

    # --- IDAT -----------------------------------------------------------
    raw = bytearray()
    for _y in range(height):
        raw.append(0)  # filter: None
        for _x in range(width):
            raw.extend((r, g, b, 255))
    compressed = zlib.compress(bytes(raw), 9)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat = (
        struct.pack(">I", len(compressed))
        + b"IDAT"
        + compressed
        + struct.pack(">I", idat_crc)
    )

    # --- IEND -----------------------------------------------------------
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)

    return sig + ihdr + idat + iend


# ---------------------------------------------------------------------------
# Source file transformations
# ---------------------------------------------------------------------------

def _transform_app_js(src: str) -> str:
    """Transform ``app.js`` → ``sidepanel.js``.

    Five exact string replacements for localStorage → chrome.storage.local,
    plus an injected boot block and topbar button event handlers.
    """
    s = src

    # 1. Boot: load token from chrome.storage instead of localStorage
    s = s.replace(
        'try { state.token = localStorage.getItem("hermeslite.token") || ""; } catch (_) {}',
        'try { const _s = await chrome.storage.local.get("token"); state.token = _s.token || ""; } catch (_) {}',
    )

    # 2. loadStatus: persist token to chrome.storage
    s = s.replace(
        'try { localStorage.setItem("hermeslite.token", state.token); } catch (_) {}',
        "try { await chrome.storage.local.set({token: state.token}); } catch (_) {}",
    )

    # 3. loadStatus: remove token
    s = s.replace(
        'try { localStorage.removeItem("hermeslite.token"); } catch (_) {}',
        'try { await chrome.storage.local.remove("token"); } catch (_) {}',
    )

    # NOTE: Theme (_initTheme / theme toggle) intentionally stays in
    # localStorage — those are synchronous call sites and injecting
    # async chrome.storage.local.get would break the module.  Theme
    # is panel-local, no cross-component sharing needed.

    # 4. Inject button handlers + chrome.storage loader.
    #    Button handlers go BEFORE the async boot IIFE (module scope)
    #    so they register even when the boot returns early.
    boot_marker = "(async () => {\n    try {\n"
    ext_handlers = (
        "// === Chrome Extension: settings + pop-out ===\n"
        'document.getElementById("btn-ext-refresh")?.addEventListener("click", () => {\n'
        "  try { location.reload(); } catch (_) {}\n"
        "});\n"
        'document.getElementById("btn-ext-settings")?.addEventListener("click", () => {\n'
        "  try { chrome.runtime.openOptionsPage(); } catch (_) {}\n"
        "});\n"
        'document.getElementById("btn-popout")?.addEventListener("click", () => {\n'
        "  try { chrome.runtime.sendMessage({ action: 'popout' }); } catch (_) {}\n"
        "});\n"
        "// === End injected handlers ===\n\n"
    )
    chrome_init = (
        ext_handlers
        + "  (async () => {\n"
        "    try {\n"
        "      // === Chrome Extension: load config from storage ===\n"
        "      try {\n"
        "        const _extCfg = await chrome.storage.local.get(['serverUrl', 'token']);\n"
        "        if (_extCfg.serverUrl) state.base = _extCfg.serverUrl;\n"
        "        if (_extCfg.token && !state.token) state.token = _extCfg.token;\n"
        "      } catch (_) {}\n"
        "      if (!state.base) {\n"
        "        document.getElementById('empty-state')?.removeAttribute('hidden');\n"
        "        const h2 = document.querySelector('#empty-state h2');\n"
        "        if (h2) h2.textContent = 'Configure server URL';\n"
        "        const p = document.querySelector('#empty-state p');\n"
        "        if (p) p.textContent = 'Click \\u2699 in the top bar to set your HermesLite server address.';\n"
        "        return;\n"
        "      }\n"
        "      // === End injected block ===\n"
    )
    s = s.replace(boot_marker, chrome_init, 1)

    return s


def _transform_style_css(src: str) -> str:
    """Prepend side-panel / floating-window overrides."""
    prefix = (
        "/* === Chrome extension: side panel / floating window overrides === */\n"
        "html, body { height: 100vh; overflow: hidden; margin: 0; padding: 0; }\n"
        "#app { height: 100vh; }\n"
        "\n"
        "/* Side panel is narrow (~400px) — reuse the mobile breakpoint layout.\n"
        "   The sidebar starts hidden and slides in via the hamburger button.\n"
        "   Just ensure the toggle button is always visible. */\n"
        "@media (max-width: 700px) {\n"
        "  #sidebar-toggle { display: inline-block !important; }\n"
        "}\n\n"
    )
    return prefix + src


def _transform_index_html(src: str) -> str:
    """Adapt ``index.html`` → ``sidepanel.html``.

    - Change resource paths from ``/style.css`` → ``sidepanel.css``
    - Change ``/app.js`` → ``sidepanel.js``
    - Inject settings + popout buttons before the theme toggle button.
    """
    s = src
    s = s.replace('href="/style.css"', 'href="sidepanel.css"')
    s = s.replace('src="/app.js"', 'src="sidepanel.js"')

    # Inject extension buttons before the theme toggle button.
    inject = (
        '<button id="btn-ext-refresh" class="icon-btn" title="Refresh">↻</button>\n'
        '        <button id="btn-ext-settings" class="icon-btn" title="Extension settings">⚙</button>\n'
        '        <button id="btn-popout" class="icon-btn" title="Pop out as window">⊞</button>\n'
        "        "
    )
    s = s.replace(
        '<button id="btn-theme" class="icon-btn" title="Toggle dark/light mode">',
        inject + '<button id="btn-theme" class="icon-btn" title="Toggle dark/light mode">',
    )
    return s


# ---------------------------------------------------------------------------
# Template files (embedded as strings)
# ---------------------------------------------------------------------------

MANIFEST_JSON = json.dumps(
    {
        "manifest_version": 3,
        "name": "HermesLite",
        "version": "1.0.0",
        "description": "HermesLite AI assistant — side panel & floating chat",
        "permissions": ["sidePanel", "storage", "clipboardWrite"],
        "host_permissions": ["http://localhost:*/*", "http://127.0.0.1:*/*"],
        "side_panel": {"default_path": "sidepanel.html"},
        "action": {
            "default_popup": "popup.html",
            "default_icon": "icons/icon16.png",
        },
        "background": {"service_worker": "background.js"},
        "options_page": "options.html",
        "icons": {
            "16": "icons/icon16.png",
            "48": "icons/icon48.png",
            "128": "icons/icon128.png",
        },
    },
    indent=2,
    ensure_ascii=False,
) + "\n"

BACKGROUND_JS = """\
// HermesLite Chrome Extension — service worker
// Default: icon click opens the side panel on the right.
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });

// Listen for "pop-out" requests from the sidepanel to open a floating window.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.action === "popout") {
    chrome.windows.create({
      url: "sidepanel.html",
      type: "popup",
      width: 420,
      height: 640,
    });
  }
});
"""

POPUP_HTML = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>
  body { width: 220px; padding: 18px; font-family: system-ui, sans-serif; margin: 0; }
  h3 { margin: 0 0 8px; font-size: 15px; }
  p { margin: 0 0 6px; font-size: 12px; color: #555; line-height: 1.45; }
  .hint { margin-top: 10px; font-size: 11px; color: #999; }
</style></head>
<body>
  <h3>◐ HermesLite</h3>
  <p>Click the extension icon to open the <b>side panel</b>.</p>
  <p>Use <b>⚙</b> inside the panel to configure your server.</p>
  <p class="hint">Right-click the icon → Options for server settings.</p>
</body>
</html>
"""

OPTIONS_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>HermesLite — Settings</title>
  <link rel="stylesheet" href="options.css">
</head>
<body>
  <div class="container">
    <h1>◐ HermesLite Settings</h1>
    <p class="subtitle">Configure the backend server connection.</p>

    <label for="serverUrl">Server URL</label>
    <input id="serverUrl" type="text" placeholder="http://localhost:8080" />
    <p class="hint">The HermesLite web server address (e.g. http://localhost:8080)</p>

    <label for="token">Auth Token <span class="optional">(optional)</span></label>
    <input id="token" type="password" placeholder="leave empty for loopback" />
    <p class="hint">Required only for non-loopback servers. Get it from the server's startup output.</p>

    <div class="actions">
      <button id="btn-test" class="secondary">Test Connection</button>
      <button id="btn-save" class="primary">Save</button>
    </div>
    <div id="status" class="status"></div>
  </div>
  <script src="options.js"></script>
</body>
</html>
"""

OPTIONS_JS = """\
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
    const base = $url.value.trim().replace(/\\/+$/, "");
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
    const base = $url.value.trim().replace(/\\/+$/, "");
    const tok = $token.value.trim();
    chrome.storage.local.set({ serverUrl: base, token: tok }, () => {
      setStatus("Settings saved.", true);
    });
  });
})();
"""

OPTIONS_CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font: 14px/1.5 system-ui, -apple-system, sans-serif;
  background: #fbfaf7; color: #1f2937;
  padding: 32px;
}
.container { max-width: 440px; }
h1 { font-size: 20px; font-weight: 600; margin-bottom: 4px; }
.subtitle { color: #6b7280; font-size: 13px; margin-bottom: 24px; }
label { display: block; font-size: 12px; font-weight: 600; color: #4b5563; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.optional { font-weight: 400; text-transform: none; color: #9ca3af; }
input {
  width: 100%; padding: 8px 10px; font-size: 14px;
  border: 1px solid #d6d2c6; border-radius: 6px;
  background: #fff; color: #1f2937; outline: none;
}
input:focus { border-color: #b45309; }
.hint { font-size: 11px; color: #9ca3af; margin: 4px 0 16px; }
.actions { display: flex; gap: 8px; margin-top: 8px; }
button {
  padding: 8px 18px; border-radius: 8px; font-size: 13px;
  font-weight: 500; cursor: pointer; font-family: inherit; border: 0;
}
button.primary { background: #b45309; color: #fffbf2; }
button.primary:hover { background: #92400e; }
button.secondary { background: transparent; color: #4b5563; border: 1px solid #d6d2c6; }
button.secondary:hover { background: #f3f1ec; }
.status { margin-top: 14px; font-size: 13px; min-height: 20px; }
.status.ok { color: #166534; }
.status.err { color: #b91c1c; }
[data-theme="dark"] {
  background: #11111b; color: #cdd6f4;
}
[data-theme="dark"] input { background: #1e1e30; border-color: #45475a; color: #cdd6f4; }
[data-theme="dark"] .subtitle, [data-theme="dark"] .hint { color: #7f849c; }
[data-theme="dark"] label { color: #a6adc8; }
[data-theme="dark"] button.secondary { color: #a6adc8; border-color: #45475a; }
[data-theme="dark"] button.secondary:hover { background: #1e1e2e; }
"""


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)


def _create_zip(src_dir: Path, zip_path: Path) -> None:
    """Create a zip archive from the extension directory."""
    import zipfile
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for fname in sorted(files):
                fpath = Path(root) / fname
                arcname = fpath.relative_to(src_dir.parent)
                zf.write(fpath, arcname)


def build() -> None:
    """Main entry point: read sources, transform, write extension files."""
    print("Building Chrome extension…")

    # Clean previous build.
    if _OUT.is_dir():
        shutil.rmtree(_OUT)
    if _ZIP_PATH.is_file():
        _ZIP_PATH.unlink()

    _OUT.mkdir(parents=True, exist_ok=True)

    # --- Read source files ------------------------------------------------
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    style_css = (_STATIC / "style.css").read_text(encoding="utf-8")
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")

    # --- Transform & write ------------------------------------------------
    _write(_OUT / "sidepanel.js", _transform_app_js(app_js))
    _write(_OUT / "sidepanel.css", _transform_style_css(style_css))
    _write(_OUT / "sidepanel.html", _transform_index_html(index_html))

    # --- Template files ---------------------------------------------------
    _write(_OUT / "manifest.json", MANIFEST_JSON)
    _write(_OUT / "background.js", BACKGROUND_JS)
    _write(_OUT / "popup.html", POPUP_HTML)
    _write(_OUT / "options.html", OPTIONS_HTML)
    _write(_OUT / "options.js", OPTIONS_JS)
    _write(_OUT / "options.css", OPTIONS_CSS)

    # --- Placeholder icons ------------------------------------------------
    r, g, b = _ICON_COLOR
    for size in (16, 48, 128):
        _write(_OUT / "icons" / f"icon{size}.png", _create_png(size, size, r, g, b))

    # --- ZIP packaging ----------------------------------------------------
    _create_zip(_OUT, _ZIP_PATH)

    # --- Summary ----------------------------------------------------------
    file_count = sum(1 for _ in _OUT.rglob("*") if _.is_file())
    print(f"  Output: {_OUT}")
    print(f"  Files:  {file_count}")
    print(f"  ZIP:    {_ZIP_PATH}")
    print("Done. Load chrome-extension/ in chrome://extensions (Developer mode).")


if __name__ == "__main__":
    build()
