/* HermesLite dashboard — zero-dependency JS

   Layout
   ------
   Left sidebar  : nav tabs + recent chats
   Right main    : one .view active at a time
                    - chat       (messages + composer)
                    - tools      (list + detail)
                    - skills     (list + detail)
                    - memory     (list + detail)
                    - config     (JSON editor)
*/

(() => {
  "use strict";

  // -----------------------------------------------------------------
  // State
  // -----------------------------------------------------------------
  const state = {
    base: "",
    token: "",
    status: null,
    sessions: [],
    activeSession: null,
    tools: [],
    activeTool: null,
    skills: [],
    activeSkill: null,
    memory: [],
    activeMemoryKey: null,
    config: null,
    envData: {},              // {NAME: VALUE} all env vars
    envPersistent: [],        // list of persistent key names
    inFlight: null,           // AbortController for the active stream
    activeView: "chat",
    commands: [],             // slash commands for autocomplete
    _slashIdx: -1,            // keyboard nav index in slash menu
    profiles: [],             // agent profiles
    activeProfile: "default", // active profile name
    attachments: [],          // [{name, data_url, is_image}]
    cronJobs: [],             // cron job list
    activeCronJob: null,      // selected cron job id
    debugMode: false,         // show LLM debug panel
    debugLog: [],             // captured debug events
  };

  // -----------------------------------------------------------------
  // HTTP helpers
  // -----------------------------------------------------------------
  function url(path) {
    if (state.token) {
      const sep = path.includes("?") ? "&" : "?";
      return state.base + path + sep + "token=" + encodeURIComponent(state.token);
    }
    return state.base + path;
  }

  async function api(method, path, body) {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    const init = { method, headers };
    if (body !== undefined) init.body = JSON.stringify(body);
    const res = await fetch(url(path), init);
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  function escapeHTML(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // -----------------------------------------------------------------
  // Simple markdown → HTML renderer (zero deps)
  // -----------------------------------------------------------------
  function renderMarkdown(text) {
    if (!text) return "";
    let html = escapeHTML(text);
    // Fenced code blocks (``` ... ```).
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
      return '<pre><code class="lang-' + (lang || "text") + '">' + code.trimEnd() + '</code></pre>';
    });
    // Pipe tables: detect table blocks (lines starting with |).
    html = html.replace(
      /(?:^|\n)((?:\|.+\|[ \t]*\n)+)/g,
      (match, block) => {
        const rows = block.trim().split("\n");
        if (rows.length < 2) return match;
        // Check for separator row (|---|---|).
        let sepIdx = -1;
        for (let i = 0; i < rows.length; i++) {
          if (/^\|[\s\-:|]+\|$/.test(rows[i].trim())) { sepIdx = i; break; }
        }
        if (sepIdx < 0) return match;
        const parseCells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        const headers = parseCells(rows[sepIdx > 0 ? 0 : sepIdx]);
        const bodyRows = rows.slice(sepIdx > 0 ? sepIdx + 1 : 1);
        let t = "<table><thead><tr>";
        headers.forEach(h => { t += "<th>" + inlineFormat(h) + "</th>"; });
        t += "</tr></thead><tbody>";
        bodyRows.forEach(row => {
          if (!row.trim()) return;
          const cells = parseCells(row);
          t += "<tr>";
          cells.forEach(c => { t += "<td>" + inlineFormat(c) + "</td>"; });
          t += "</tr>";
        });
        t += "</tbody></table>";
        return "\n" + t + "\n";
      }
    );
    // Inline code (`...`).
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    // Bold (**...**).
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    // Italic (*...*).
    html = html.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");
    // Strikethrough (~~...~~).
    html = html.replace(/~~(.+?)~~/g, "<del>$1</del>");
    // Headings.
    html = html.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
    html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
    html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
    html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
    // Unordered lists.
    html = html.replace(/^[\-\*] (.+)$/gm, "<li>$1</li>");
    html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, "<ul>$1</ul>");
    // Ordered lists.
    html = html.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");
    // Blockquotes.
    html = html.replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>");
    // Horizontal rules.
    html = html.replace(/^---+$/gm, "<hr>");
    // Links [text](url).
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // Line breaks → paragraphs (double newline = paragraph, single = <br>).
    html = html.replace(/\n{2,}/g, "</p><p>");
    html = html.replace(/\n/g, "<br>");
    html = "<p>" + html + "</p>";
    // Clean up empty paragraphs.
    html = html.replace(/<p>\s*<\/p>/g, "");
    // Fix block elements that got wrapped in <p>.
    html = html.replace(/<p>(<(?:pre|h[1-6]|ul|ol|blockquote|hr|table)[^>]*>)/g, "$1");
    html = html.replace(/(<\/(?:pre|h[1-6]|ul|ol|blockquote|hr|table)>)<\/p>/g, "$1");
    return html;
  }

  /** Apply inline formatting to a single cell/fragment. */
  function inlineFormat(s) {
    if (!s) return "";
    let h = escapeHTML(s);
    h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");
    h = h.replace(/~~(.+?)~~/g, "<del>$1</del>");
    return h;
  }

  // -----------------------------------------------------------------
  // View switching (sidebar nav → main panel)
  // -----------------------------------------------------------------
  function switchView(name) {
    state.activeView = name;
    // Stop cron polling when leaving cron view.
    if (name !== "cron" && _cronPollTimer) {
      clearInterval(_cronPollTimer);
      _cronPollTimer = null;
      _cronPollSeenRunning = false;
    }
    document.querySelectorAll(".nav-item").forEach(b => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    document.querySelectorAll(".view").forEach(v => {
      v.classList.toggle("active", v.dataset.view === name);
    });
    // Lazy-refresh data the user is now looking at.
    if (name === "memory") loadMemory();
    if (name === "config") loadConfig();
    if (name === "env") loadEnv();
    if (name === "cron") loadCronJobs();
  }

  document.querySelectorAll(".nav-item").forEach(btn => {
    btn.addEventListener("click", () => {
      switchView(btn.dataset.view);
      closeSidebar();
    });
  });

  // -----------------------------------------------------------------
  // Sidebar toggle (mobile)
  // -----------------------------------------------------------------
  const sidebar = document.getElementById("sidebar");
  const overlay = document.getElementById("sidebar-overlay");
  const toggleBtn = document.getElementById("sidebar-toggle");

  function openSidebar() {
    sidebar.classList.add("open");
    overlay.classList.add("show");
  }
  function closeSidebar() {
    sidebar.classList.remove("open");
    overlay.classList.remove("show");
  }

  if (toggleBtn) toggleBtn.addEventListener("click", () => {
    sidebar.classList.contains("open") ? closeSidebar() : openSidebar();
  });
  if (overlay) overlay.addEventListener("click", closeSidebar);

  // -----------------------------------------------------------------
  // Sessions (sidebar list)
  // -----------------------------------------------------------------
  async function loadSessions() {
    state.sessions = await api("GET", "/api/sessions?limit=100");
    renderSessions();
  }

  function renderSessions() {
    const list = document.getElementById("session-list");
    list.innerHTML = "";
    // Filter out internal sessions (cron runs, delegate subagents).
    const visible = state.sessions.filter(s =>
      !s.id.startsWith("cron_") && !s.id.startsWith("delegate-")
    );
    if (!visible.length) {
      const li = document.createElement("li");
      li.className = "empty-row";
      li.textContent = "(no chats yet)";
      list.appendChild(li);
      return;
    }
    visible.forEach(s => {
      const li = document.createElement("li");
      if (s.id === state.activeSession) li.classList.add("active");
      const title = document.createElement("span");
      title.className = "name";
      title.textContent = s.title || s.id.slice(0, 8);
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = formatTime(s.updated_at);
      const del = document.createElement("span");
      del.className = "del";
      del.textContent = "×";
      del.title = "Delete session";
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm("Delete this session?")) return;
        await api("DELETE", "/api/sessions/" + s.id);
        if (state.activeSession === s.id) {
          state.activeSession = null;
          renderMessagesEmpty();
        }
        await loadSessions();
      });
      li.appendChild(title);
      li.appendChild(meta);
      li.appendChild(del);
      li.addEventListener("click", () => activateSession(s.id));
      list.appendChild(li);
    });
  }

  function formatTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const today = new Date();
    if (d.toDateString() === today.toDateString()) {
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleDateString();
  }

  async function activateSession(id) {
    state.activeSession = id;
    renderSessions();
    switchView("chat");
    const msgs = await api("GET", "/api/sessions/" + id + "/messages");
    renderMessagesFromState(msgs);
    // Fetch last turn's usage for the context progress bar.
    try {
      const usage = await api("GET", "/api/sessions/" + id + "/usage");
      if (usage && (usage.prompt_tokens || 0) > 0) updateUsage(usage);
    } catch (_) {}
  }

  document.getElementById("btn-new-session").addEventListener("click", async () => {
    // Reset the active session; the next ``send()`` will create a new
    // one. We don't pre-create it here because the title depends on the
    // user's first message, and we want one less round trip.
    state.activeSession = null;
    await loadSessions();
    switchView("chat");
    renderMessagesFromState([]);
  });

  // -----------------------------------------------------------------
  // Messages / chat view
  //
  // Phase-based bubble management. During a streaming turn, content
  // arrives in phases: "thinking" (reasoning_content deltas), "text"
  // (assistant_text_delta), and "tool" (tool_call / tool_result).
  //
  // Each phase change finalises the previous bubble and opens a new
  // one. This ensures thinking blocks, text blocks, and tool blocks
  // are always visually separated — even if the model alternates
  // between thinking and text multiple times in one turn.
  // -----------------------------------------------------------------
  let _phase = null;      // "thinking" | "text" | null
  let _currentEl = null;  // the DOM element accumulating content

  function showEmpty(show) {
    const empty = document.getElementById("empty-state");
    if (!empty) return;
    if (show) empty.removeAttribute("hidden");
    else empty.setAttribute("hidden", "");
  }

  function renderMessagesEmpty() {
    const el = document.getElementById("messages");
    el.innerHTML = "";
    _phase = null;
    _currentEl = null;
    showEmpty(true);
  }

  function renderMessagesFromState(msgs) {
    const el = document.getElementById("messages");
    el.innerHTML = "";
    _phase = null;
    _currentEl = null;
    if (!state.activeSession) {
      showEmpty(true);
      return;
    }
    showEmpty(false);
    msgs.forEach(m => appendMessageDOM(m));
    scrollToBottom();
  }

  function appendMessageDOM(m) {
    const el = document.getElementById("messages");

    // Tool results: collapsible <details>, default collapsed.
    if (m.role === "tool") {
      const details = document.createElement("details");
      details.className = "msg tool-result";
      if (m.id) details.dataset.mid = m.id;

      const summary = document.createElement("summary");
      summary.className = "tool-summary";
      const marker = document.createElement("span");
      marker.className = "tool-marker";
      marker.textContent = "\u25B6";
      summary.appendChild(marker);
      const label = document.createElement("span");
      label.className = "tool-name";
      label.textContent = m.name || "tool result";
      summary.appendChild(label);
      const preview = document.createElement("span");
      preview.className = "tool-args-preview";
      const text = String(m.content || "");
      preview.textContent = text.length > 80 ? text.slice(0, 80) + "\u2026" : text;
      summary.appendChild(preview);
      details.appendChild(summary);

      const body = document.createElement("div");
      body.className = "body";
      body.textContent = text;
      details.appendChild(body);
      el.appendChild(details);
      return;
    }

    // All other messages: plain <div>.
    // Skip empty assistant messages (tool-call carriers with no visible text).
    if (m.role === "assistant") {
      const c = m.content;
      const hasText = Array.isArray(c)
        ? c.some(p => p.type === "text" && p.text)
        : !!c;
      if (!hasText) return;
    }
    const div = document.createElement("div");
    div.className = "msg " + (
      m.role === "user" ? "user" :
      m.role === "assistant" ? "assistant" : "system"
    );
    if (m.id) div.dataset.mid = m.id;

    const role = document.createElement("span");
    role.className = "role";
    role.textContent = m.role;
    div.appendChild(role);

    const body = document.createElement("div");
    body.className = "body";
    // content may be string or array of parts (multimodal).
    const content = m.content;
    let textContent = "";
    if (Array.isArray(content)) {
      for (const part of content) {
        if (part.type === "text" && part.text) {
          textContent += part.text;
        } else if (part.type === "image_url") {
          const url = (part.image_url && part.image_url.url) || "";
          if (url) {
            const img = document.createElement("img");
            img.src = url;
            img.alt = "attached image";
            body.appendChild(img);
          }
        }
      }
    } else {
      textContent = content || "";
    }
    if (textContent) {
      body.innerHTML = renderMarkdown(textContent);
    }
    div.appendChild(body);

    // Action buttons for user/assistant messages only.
    const isTextMsg = m.role === "user" || m.role === "assistant";
    if (isTextMsg && textContent) {
      const actions = document.createElement("div");
      actions.className = "msg-actions";

      // Markdown toggle.
      const btnMd = document.createElement("button");
      btnMd.className = "msg-action";
      btnMd.textContent = "MD";
      btnMd.title = "Toggle markdown / raw text";
      let mdMode = true;
      btnMd.addEventListener("click", () => {
        mdMode = !mdMode;
        btnMd.classList.toggle("active", mdMode);
        body.innerHTML = mdMode ? renderMarkdown(textContent) : escapeHTML(textContent);
      });
      btnMd.classList.add("active");
      actions.appendChild(btnMd);

      // Copy.
      const btnCopy = document.createElement("button");
      btnCopy.className = "msg-action";
      btnCopy.textContent = "\u2398";
      btnCopy.title = "Copy to clipboard";
      btnCopy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(textContent);
          btnCopy.textContent = "\u2713";
          setTimeout(() => { btnCopy.textContent = "\u2398"; }, 1200);
        } catch (_) {}
      });
      actions.appendChild(btnCopy);

      // Delete (only for persisted messages with an id).
      if (m.id && state.activeSession) {
        const btnDel = document.createElement("button");
        btnDel.className = "msg-action";
        btnDel.textContent = "\u2715";
        btnDel.title = "Delete this message";
        btnDel.addEventListener("click", async () => {
          if (!confirm("Delete this message?")) return;
          if (!confirm("Are you sure? This cannot be undone.")) return;
          try {
            await api("DELETE", "/api/sessions/" + state.activeSession + "/messages/" + m.id);
            div.remove();
          } catch (err) {
            alert("Delete failed: " + err.message);
          }
        });
        actions.appendChild(btnDel);
      }

      div.appendChild(actions);
    }

    el.appendChild(div);
    return div;
  }

  /** Finalise whatever phase is active (remove streaming cursor, etc). */
  function _finalizePhase() {
    if (_currentEl && _phase === "text" && _currentEl.textContent) {
      _currentEl.innerHTML = renderMarkdown(_currentEl.textContent);
    }
    if (_currentEl) {
      const wrap = _currentEl.closest ? _currentEl.closest(".msg") : _currentEl.parentElement;
      if (wrap) wrap.classList.remove("streaming");
    }
    _phase = null;
    _currentEl = null;
  }

  // -----------------------------------------------------------------
  // Phase entry points — each one finalises the previous phase and
  // opens a new element if the phase is different.
  // -----------------------------------------------------------------

  function _enterThinking() {
    if (_phase === "thinking") return;  // same phase — keep appending
    _finalizePhase();
    // Create a collapsible thinking details block.
    const el = document.getElementById("messages");
    const details = document.createElement("details");
    details.className = "msg thinking streaming";
    const summary = document.createElement("summary");
    summary.className = "tool-summary";
    const marker = document.createElement("span");
    marker.className = "tool-marker";
    marker.textContent = "\u2728";
    summary.appendChild(marker);
    const label = document.createElement("span");
    label.className = "tool-name";
    label.textContent = "thinking";
    summary.appendChild(label);
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "body";
    details.appendChild(body);
    el.appendChild(details);
    _phase = "thinking";
    _currentEl = body;
    scrollToBottom();
  }

  function _enterText() {
    if (_phase === "text") return;  // same phase — keep appending
    _finalizePhase();
    // Create a standard assistant text bubble.
    const el = document.getElementById("messages");
    const div = document.createElement("div");
    div.className = "msg assistant streaming";
    const roleEl = document.createElement("span");
    roleEl.className = "role";
    roleEl.textContent = "assistant";
    div.appendChild(roleEl);
    const body = document.createElement("div");
    body.className = "body";
    div.appendChild(body);
    el.appendChild(div);
    _phase = "text";
    _currentEl = body;
    scrollToBottom();
  }

  function _enterTool() {
    // Tool events always finalise the current phase.
    _finalizePhase();
  }

  /** Show the system prompt in a collapsible details block. */
  function _showSystemPrompt(text) {
    if (!text) return;
    const el = document.getElementById("messages");
    const details = document.createElement("details");
    details.className = "msg system-prompt";
    const summary = document.createElement("summary");
    summary.className = "tool-summary";
    const marker = document.createElement("span");
    marker.className = "tool-marker";
    marker.textContent = "\u2699";
    summary.appendChild(marker);
    const label = document.createElement("span");
    label.className = "tool-name";
    label.textContent = "System Prompt";
    summary.appendChild(label);
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "body";
    body.textContent = text;
    details.appendChild(body);
    el.appendChild(details);
    scrollToBottom();
  }

  // -----------------------------------------------------------------
  // Tool block appenders (called after _enterTool)
  // -----------------------------------------------------------------

  function appendToolCall(name, args) {
    const el = document.getElementById("messages");
    const details = document.createElement("details");
    details.className = "msg tool-call";
    const summary = document.createElement("summary");
    summary.className = "tool-summary";
    const marker = document.createElement("span");
    marker.className = "tool-marker";
    marker.textContent = "\u25B6";
    summary.appendChild(marker);
    const toolName = document.createElement("span");
    toolName.className = "tool-name";
    toolName.textContent = name;
    summary.appendChild(toolName);
    const preview = document.createElement("span");
    preview.className = "tool-args-preview";
    const argStr = JSON.stringify(args);
    preview.textContent = argStr.length > 80 ? argStr.slice(0, 80) + "\u2026" : argStr;
    summary.appendChild(preview);
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "body";
    body.textContent = JSON.stringify(args, null, 2);
    details.appendChild(body);
    el.appendChild(details);
    scrollToBottom();
  }

  function appendToolResult(ok, data) {
    const el = document.getElementById("messages");
    const details = document.createElement("details");
    details.className = "msg tool-result";
    const summary = document.createElement("summary");
    summary.className = "tool-summary";
    const marker = document.createElement("span");
    marker.className = "tool-marker";
    marker.textContent = ok ? "\u2713" : "\u2717";
    summary.appendChild(marker);
    const label = document.createElement("span");
    label.className = "tool-name";
    label.textContent = ok ? "result" : "error";
    summary.appendChild(label);
    const preview = document.createElement("span");
    preview.className = "tool-args-preview";
    const text = String(data || "");
    preview.textContent = text.length > 80 ? text.slice(0, 80) + "\u2026" : text;
    summary.appendChild(preview);
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "body";
    // Show full content — server-side already persists results >100K to disk.
    body.textContent = String(data || "");
    details.appendChild(body);
    el.appendChild(details);
    scrollToBottom();
  }

  function scrollToBottom() {
    const el = document.getElementById("messages");
    if (el) el.scrollTop = el.scrollHeight;
  }

  // -----------------------------------------------------------------
  // Sending
  // -----------------------------------------------------------------
  document.getElementById("btn-send").addEventListener("click", send);
  document.getElementById("btn-cancel").addEventListener("click", () => {
    if (state.inFlight) state.inFlight.abort();
  });
  document.getElementById("input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      send();
    }
  });

  // -----------------------------------------------------------------
  // Slash command autocomplete
  // -----------------------------------------------------------------
  const slashMenu = document.getElementById("slash-menu");
  let _slashVisible = false;

  function showSlashMenu(filter) {
    const q = (filter || "").toLowerCase();
    const matches = q
      ? state.commands.filter(c => c.name.toLowerCase().startsWith(q))
      : state.commands;
    if (!matches.length) { hideSlashMenu(); return; }
    slashMenu.innerHTML = "";
    state._slashIdx = 0;
    matches.forEach((c, i) => {
      const div = document.createElement("div");
      div.className = "slash-item" + (i === 0 ? " active" : "");
      const name = document.createElement("span");
      name.className = "cmd-name";
      name.textContent = c.name;
      const desc = document.createElement("span");
      desc.className = "cmd-desc";
      desc.textContent = c.description;
      div.appendChild(name);
      div.appendChild(desc);
      div.addEventListener("click", () => _selectSlash(c.name + " "));
      div.addEventListener("mouseenter", () => {
        slashMenu.querySelectorAll(".slash-item").forEach(el => el.classList.remove("active"));
        div.classList.add("active");
        state._slashIdx = i;
      });
      slashMenu.appendChild(div);
    });
    _slashVisible = true;
    slashMenu.removeAttribute("hidden");
  }

  function hideSlashMenu() {
    slashMenu.setAttribute("hidden", "");
    _slashVisible = false;
    state._slashIdx = -1;
  }

  function _selectSlash(name) {
    const input = document.getElementById("input");
    input.value = name;
    hideSlashMenu();
    input.focus();
  }

  document.getElementById("input").addEventListener("input", () => {
    const val = document.getElementById("input").value;
    if (val.startsWith("/") && !val.includes("\n")) {
      showSlashMenu(val);
    } else {
      hideSlashMenu();
    }
  });

  document.getElementById("input").addEventListener("keydown", (ev) => {
    if (!_slashVisible) return;
    const items = slashMenu.querySelectorAll(".slash-item");
    if (!items.length) return;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      state._slashIdx = Math.min(state._slashIdx + 1, items.length - 1);
      items.forEach((el, i) => el.classList.toggle("active", i === state._slashIdx));
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      state._slashIdx = Math.max(state._slashIdx - 1, 0);
      items.forEach((el, i) => el.classList.toggle("active", i === state._slashIdx));
    } else if (ev.key === "Tab" || ev.key === "Enter" && state._slashIdx >= 0) {
      ev.preventDefault();
      const cmdName = state.commands[
        [...slashMenu.querySelectorAll(".slash-item")].findIndex(el => el.classList.contains("active"))
      ];
      if (cmdName) _selectSlash(cmdName.name + " ");
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      hideSlashMenu();
    }
  });

  // -----------------------------------------------------------------
  // File attachments (paste, drag-drop, file picker)
  // -----------------------------------------------------------------
  const fileInput = document.getElementById("file-input");
  const attPreview = document.getElementById("attachment-preview");

  function _isImageFile(file) {
    return file.type && file.type.startsWith("image/");
  }

  function _readAsDataURL(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  async function _addAttachment(file) {
    if (file.size > 10 * 1024 * 1024) {
      alert("File too large (max 10 MB): " + file.name);
      return;
    }
    const dataUrl = await _readAsDataURL(file);
    state.attachments.push({
      name: file.name,
      data_url: dataUrl,
      is_image: _isImageFile(file),
    });
    _renderAttachments();
  }

  function _removeAttachment(idx) {
    state.attachments.splice(idx, 1);
    _renderAttachments();
  }

  function _renderAttachments() {
    attPreview.innerHTML = "";
    if (!state.attachments.length) {
      attPreview.setAttribute("hidden", "");
      return;
    }
    attPreview.removeAttribute("hidden");
    state.attachments.forEach((att, i) => {
      const div = document.createElement("div");
      div.className = "att-item";
      if (att.is_image) {
        const img = document.createElement("img");
        img.src = att.data_url;
        div.appendChild(img);
      }
      const name = document.createElement("span");
      name.className = "att-name";
      name.textContent = att.name;
      div.appendChild(name);
      const rm = document.createElement("span");
      rm.className = "att-remove";
      rm.textContent = "×";
      rm.addEventListener("click", () => _removeAttachment(i));
      div.appendChild(rm);
      attPreview.appendChild(div);
    });
  }

  // File picker button.
  document.getElementById("btn-attach").addEventListener("click", () => {
    fileInput.click();
  });
  fileInput.addEventListener("change", async () => {
    for (const file of fileInput.files) {
      await _addAttachment(file);
    }
    fileInput.value = "";
  });

  // Paste handler: detect pasted images from clipboard.
  document.getElementById("input").addEventListener("paste", async (ev) => {
    const items = ev.clipboardData && ev.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.type && item.type.startsWith("image/")) {
        ev.preventDefault();
        const file = item.getAsFile();
        if (file) await _addAttachment(file);
        break;
      }
    }
  });

  // Drag-and-drop handler on the textarea.
  const inputEl = document.getElementById("input");
  inputEl.addEventListener("dragover", (ev) => {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
  });
  inputEl.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    const files = ev.dataTransfer.files;
    for (const file of files) {
      await _addAttachment(file);
    }
  });

  // Close slash menu on outside click.
  document.addEventListener("click", (ev) => {
    if (_slashVisible && !slashMenu.contains(ev.target) && ev.target.id !== "input") {
      hideSlashMenu();
    }
  });

  async function send() {
    const input = document.getElementById("input");
    const text = input.value.trim();
    if (!text && !state.attachments.length) return;
    if (state.inFlight) return;  // double-send guard
    hideSlashMenu();
    const attachments = state.attachments.splice(0);  // move + clear

    // Auto-create a session if none is active.
    let firstTurnInNewSession = false;
    if (!state.activeSession) {
      const s = await api("POST", "/api/sessions", { title: text.slice(0, 40) });
      state.activeSession = s.id;
      firstTurnInNewSession = true;
    }

    switchView("chat");

    // Always reload history from server before appending the new turn.
    // This way we don't lose the previous turns when the user opens an
    // old session and immediately starts typing.
    if (firstTurnInNewSession) {
      renderMessagesFromState([]);  // brand-new session: no history
    } else {
      try {
        const history = await api("GET", "/api/sessions/" + state.activeSession + "/messages");
        renderMessagesFromState(history);
      } catch (err) {
        // If the history fetch fails we still continue — the new turn
        // will succeed; we just don't have the previous context shown.
        console.warn("send: could not load history:", err);
      }
    }

    input.value = "";
    _renderAttachments();  // clear preview
    // Render user message with text + images.
    if (attachments.length) {
      const parts = [{ type: "text", text: text }];
      for (const att of attachments) {
        if (att.is_image) parts.push({ type: "image_url", image_url: { url: att.data_url } });
      }
      appendMessageDOM({ role: "user", content: parts });
    } else {
      appendMessageDOM({ role: "user", content: text });
    }

    const ac = new AbortController();
    state.inFlight = ac;
    document.getElementById("btn-send").hidden = true;
    document.getElementById("btn-cancel").hidden = false;

    try {
      const res = await fetch(url("/api/sessions/" + state.activeSession + "/chat/stream"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(state.token ? { "Authorization": "Bearer " + state.token } : {}) },
        body: JSON.stringify({ content: text, attachments: attachments.map(a => ({ data_url: a.data_url, name: a.name })) }),
        signal: ac.signal,
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
        _enterText();
        _currentEl.textContent = "Error: " + detail;
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buf.indexOf("\n\n")) !== -1) {
          const frame = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          handleFrame(frame);
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        _enterText();
        _currentEl.textContent = "Error: " + err.message;
      }
    } finally {
      _finalizePhase();
      state.inFlight = null;
      document.getElementById("btn-send").hidden = false;
      document.getElementById("btn-cancel").hidden = true;
      scrollToBottom();
      loadSessions();  // refresh updated_at
    }
  }

  function handleFrame(frame) {
    const lines = frame.split("\n");
    let payload = null;
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        payload = line.slice(6);
        break;
      }
    }
    if (!payload) return;
    let evt;
    try { evt = JSON.parse(payload); } catch (_) { return; }

    if (evt.type === "ping") return;

    // Debug panel capture: record all non-ping events.
    if (state.debugMode && evt.type !== "ping") {
      state.debugLog.push(evt);
      _renderDebugEntry(evt);
    }

    if (evt.type === "system_prompt") {
      _enterTool();
      _showSystemPrompt(evt.text);

    } else if (evt.type === "thinking_content") {
      _enterThinking();
      _currentEl.textContent += evt.text;
      scrollToBottom();

    } else if (evt.type === "assistant_text_delta") {
      _enterText();
      _currentEl.textContent += evt.text;
      scrollToBottom();

    } else if (evt.type === "assistant_text_done") {
      if (_phase === "text" && _currentEl && typeof evt.text === "string"
          && evt.text.length > (_currentEl.textContent || "").length) {
        _currentEl.innerHTML = renderMarkdown(evt.text);
        scrollToBottom();
      }

    } else if (evt.type === "tool_call") {
      _enterTool();
      appendToolCall(evt.name, evt.args);

    } else if (evt.type === "tool_result") {
      _enterTool();
      appendToolResult(evt.ok, evt.data);

    } else if (evt.type === "approval_request") {
      _showApprovalDialog(evt.approval_id, evt.command, evt.description);

    } else if (evt.type === "sudo_request") {
      _showSudoDialog(evt.request_id, evt.command);

    } else if (evt.type === "command_result") {
      _enterText();
      _currentEl.textContent = evt.text || "";

    } else if (evt.type === "retry") {
      // /retry: clear chat and put original message back in input.
      renderMessagesFromState([]);
      const input = document.getElementById("input");
      if (input && evt.text) {
        input.value = evt.text;
        input.focus();
      }

    } else if (evt.type === "branch") {
      // /branch: switch to the new branched session.
      state.activeSession = evt.session_id;
      renderMessagesFromState([]);
      loadSessions();

    } else if (evt.type === "new_session") {
      state.activeSession = null;
      renderMessagesFromState([]);

    } else if (evt.type === "clear") {
      renderMessagesEmpty();

    } else if (evt.type === "done") {
      if (_phase === "text" && _currentEl && evt.text && !(_currentEl.textContent || "").length) {
        _currentEl.innerHTML = renderMarkdown(evt.text);
        scrollToBottom();
      }
      if (evt.usage) updateUsage(evt.usage);

    } else if (evt.type === "error") {
      _enterText();
      _currentEl.textContent = "Error: " + (evt.detail || "unknown");

    } else if (evt.type === "retry_status") {
      _enterText();
      _currentEl.textContent = "Retrying (attempt " + evt.attempt + "/" + evt.max_attempts + ") in " + evt.wait_seconds + "s...";

    } else if (evt.type === "subagent_event") {
      _handleSubagentEvent(evt);
    }
  }

  // -----------------------------------------------------------------
  // Sub-agent event handling
  // -----------------------------------------------------------------

  // Track active sub-agent containers by subagent_id.
  const _subagentEls = {};

  function _handleSubagentEvent(evt) {
    const sid = evt.subagent_id;
    const kind = evt.event_kind;
    const payload = evt.payload || {};

    if (kind === "subagent.start") {
      _finalizePhase();
      const el = document.getElementById("messages");
      const details = document.createElement("details");
      details.className = "msg subagent streaming";
      details.open = true;
      const summary = document.createElement("summary");
      const icon = document.createElement("span");
      icon.className = "sa-icon";
      icon.textContent = "\uD83D\uDD00";
      summary.appendChild(icon);
      const goal = document.createElement("span");
      goal.className = "sa-goal";
      goal.textContent = payload.goal || evt.goal || "sub-agent task";
      summary.appendChild(goal);
      const status = document.createElement("span");
      status.className = "sa-status running";
      status.textContent = "running";
      summary.appendChild(status);
      details.appendChild(summary);
      const body = document.createElement("div");
      body.className = "sa-body";
      const toolsDiv = document.createElement("div");
      toolsDiv.className = "sa-tools";
      body.appendChild(toolsDiv);
      details.appendChild(body);
      el.appendChild(details);
      _subagentEls[sid] = { details, body, toolsDiv, status };
      scrollToBottom();

    } else if (kind === "subagent.complete") {
      const el = _subagentEls[sid];
      if (el) {
        el.details.classList.remove("streaming");
        el.status.textContent = payload.status || "done";
        el.status.className = "sa-status " + (payload.status || "completed");
        if (payload.summary) {
          const sumDiv = document.createElement("div");
          sumDiv.className = "sa-summary";
          sumDiv.textContent = payload.summary;
          el.body.appendChild(sumDiv);
        }
        scrollToBottom();
        delete _subagentEls[sid];
      }

    } else if (kind === "assistant_text_delta" || kind === "assistant_text_done") {
      const el = _subagentEls[sid];
      if (el && payload.text) {
        // Append streaming text to the sub-agent body.
        let textNode = el.body.querySelector(".sa-stream-text");
        if (!textNode) {
          textNode = document.createElement("div");
          textNode.className = "sa-summary sa-stream-text";
          el.body.appendChild(textNode);
        }
        textNode.textContent += payload.text;
        scrollToBottom();
      }

    } else if (kind === "tool_call") {
      const el = _subagentEls[sid];
      if (el) {
        const item = document.createElement("div");
        item.className = "sa-tool-item";
        const name = document.createElement("span");
        name.className = "sa-tool-name";
        name.textContent = payload.name || "tool";
        item.appendChild(name);
        const preview = document.createElement("span");
        preview.className = "sa-tool-preview";
        const argStr = JSON.stringify(payload.args || {});
        preview.textContent = argStr.length > 60 ? argStr.slice(0, 60) + "\u2026" : argStr;
        item.appendChild(preview);
        el.toolsDiv.appendChild(item);
        scrollToBottom();
      }

    } else if (kind === "tool_result") {
      // Optionally show tool results in the sub-agent body.
      // Keep it minimal — just update the last tool item's preview.
      const el = _subagentEls[sid];
      if (el) {
        const items = el.toolsDiv.querySelectorAll(".sa-tool-item");
        const last = items[items.length - 1];
        if (last) {
          const preview = last.querySelector(".sa-tool-preview");
          if (preview) {
            const text = String(payload.data || "");
            preview.textContent = (payload.ok ? "\u2713 " : "\u2717 ") +
              (text.length > 50 ? text.slice(0, 50) + "\u2026" : text);
          }
        }
      }
    }
  }

  // -----------------------------------------------------------------
  // Approval dialog
  // -----------------------------------------------------------------
  function _showApprovalDialog(approvalId, command, description) {
    const overlay = document.createElement("div");
    overlay.className = "approval-overlay";
    const dialog = document.createElement("div");
    dialog.className = "approval-dialog";
    const title = document.createElement("h3");
    title.textContent = "⚠ Dangerous Command";
    dialog.appendChild(title);
    const desc = document.createElement("p");
    desc.className = "approval-desc";
    desc.textContent = description || "This command requires approval.";
    dialog.appendChild(desc);
    const cmd = document.createElement("pre");
    cmd.className = "approval-cmd";
    cmd.textContent = command;
    dialog.appendChild(cmd);
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const denyBtn = document.createElement("button");
    denyBtn.className = "secondary";
    denyBtn.textContent = "Deny";
    denyBtn.addEventListener("click", async () => {
      await api("POST", "/api/approve", { approval_id: approvalId, decision: "deny" });
      overlay.remove();
    });
    const allowBtn = document.createElement("button");
    allowBtn.className = "primary";
    allowBtn.textContent = "Allow Once";
    allowBtn.addEventListener("click", async () => {
      await api("POST", "/api/approve", { approval_id: approvalId, decision: "allow" });
      overlay.remove();
    });
    actions.appendChild(denyBtn);
    actions.appendChild(allowBtn);
    dialog.appendChild(actions);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
  }

  // -----------------------------------------------------------------
  // Sudo password dialog
  // -----------------------------------------------------------------
  function _showSudoDialog(requestId, command) {
    const overlay = document.createElement("div");
    overlay.className = "approval-overlay";
    const dialog = document.createElement("div");
    dialog.className = "approval-dialog";

    const title = document.createElement("h3");
    title.textContent = "SUDO PASSWORD REQUIRED";
    dialog.appendChild(title);

    const desc = document.createElement("p");
    desc.className = "approval-desc";
    desc.textContent = "Enter the sudo password to execute this command. The password is cached for this session and never sent to the AI model.";
    dialog.appendChild(desc);

    if (command) {
      const cmd = document.createElement("pre");
      cmd.className = "approval-cmd";
      cmd.textContent = command;
      dialog.appendChild(cmd);
    }

    const pwInput = document.createElement("input");
    pwInput.type = "password";
    pwInput.placeholder = "sudo password";
    pwInput.className = "sudo-input";
    pwInput.autocomplete = "off";
    dialog.appendChild(pwInput);

    const rejectReason = document.createElement("input");
    rejectReason.type = "text";
    rejectReason.placeholder = "Reason for rejecting (optional)";
    rejectReason.className = "sudo-input";
    rejectReason.style.marginTop = "6px";
    dialog.appendChild(rejectReason);

    const errorMsg = document.createElement("div");
    errorMsg.className = "sudo-error";
    errorMsg.style.display = "none";
    dialog.appendChild(errorMsg);

    const actions = document.createElement("div");
    actions.className = "approval-actions";

    const rejectBtn = document.createElement("button");
    rejectBtn.className = "secondary";
    rejectBtn.textContent = "Reject";
    rejectBtn.addEventListener("click", async () => {
      const reason = rejectReason.value.trim();
      await api("POST", "/api/sudo", {
        request_id: requestId,
        action: "reject",
        message: reason,
      });
      overlay.remove();
    });

    const submitBtn = document.createElement("button");
    submitBtn.className = "primary";
    submitBtn.textContent = "Submit";
    submitBtn.addEventListener("click", async () => {
      const pw = pwInput.value;
      if (!pw) {
        errorMsg.textContent = "Please enter a password or click Reject.";
        errorMsg.style.display = "block";
        return;
      }
      await api("POST", "/api/sudo", {
        request_id: requestId,
        action: "password",
        password: pw,
      });
      overlay.remove();
    });

    // Enter key submits
    pwInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        submitBtn.click();
      }
    });

    actions.appendChild(rejectBtn);
    actions.appendChild(submitBtn);
    dialog.appendChild(actions);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    pwInput.focus();
  }

  function updateUsage(u) {
    const el = document.getElementById("usage");
    if (!el) return;
    const maxCtx = u.max_context_tokens || 0;
    const promptTok = u.prompt_tokens || 0;
    const totalTok = u.total_tokens || 0;
    if (maxCtx > 0) {
      const pct = Math.min(100, Math.round(promptTok / maxCtx * 100));
      const filled = Math.round(pct / 10);
      const bar = "\u2588".repeat(filled) + "\u2591".repeat(10 - filled);
      el.textContent = promptTok + "/" + maxCtx + " [" + bar + "] " + pct + "%";
    } else {
      el.textContent = totalTok + " tok";
    }
  }

  // -----------------------------------------------------------------
  // Tools (list + detail)
  // -----------------------------------------------------------------
  async function loadTools() {
    state.tools = await api("GET", "/api/tools");
    document.getElementById("count-tools").textContent = state.tools.length;
    renderToolList();
  }

  function renderToolList(filter) {
    const list = document.getElementById("tool-list");
    list.innerHTML = "";
    const q = (filter || "").toLowerCase();
    const filtered = q
      ? state.tools.filter(t =>
          (t.name || "").toLowerCase().includes(q) ||
          (t.description || "").toLowerCase().includes(q))
      : state.tools;
    if (!filtered.length) {
      const li = document.createElement("li");
      li.className = "empty-row";
      li.textContent = q ? "(no tools match)" : "(no tools registered)";
      list.appendChild(li);
      return;
    }
    filtered.forEach(t => {
      const li = document.createElement("li");
      if (t.name === state.activeTool) li.classList.add("active");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = t.name;
      const desc = document.createElement("span");
      desc.className = "desc";
      desc.textContent = t.description || "";
      li.appendChild(name);
      li.appendChild(desc);
      li.addEventListener("click", () => selectTool(t.name));
      list.appendChild(li);
    });
  }

  function selectTool(name) {
    state.activeTool = name;
    renderToolList(document.getElementById("tool-filter").value);
    const tool = state.tools.find(t => t.name === name);
    const detail = document.getElementById("tool-detail");
    if (!tool) {
      detail.innerHTML = '<div class="placeholder">Tool not found.</div>';
      return;
    }
    const params = tool.parameters || { type: "object", properties: {} };
    const required = params.required || [];
    const props = params.properties || {};
    const propRows = Object.entries(props).map(([k, v]) => {
      const type = (v && v.type) || "?";
      const desc = (v && v.description) || "";
      const req = required.includes(k) ? '<span class="badge">required</span>' : '<span class="badge">optional</span>';
      return `<tr>
        <td><code>${escapeHTML(k)}</code></td>
        <td>${escapeHTML(type)}</td>
        <td>${req}</td>
        <td>${escapeHTML(desc)}</td>
      </tr>`;
    }).join("") || '<tr><td colspan="4" style="color:#9ca3af;font-style:italic">no parameters</td></tr>';

    detail.innerHTML = `
      <h2>${escapeHTML(tool.name)}</h2>
      <div class="subtitle">${escapeHTML(tool.description || "")}</div>
      <div class="meta">
        <span class="badge">tool</span>
        <span>${Object.keys(props).length} parameter(s)</span>
      </div>
      <h3>Parameters</h3>
      <table class="params">
        <thead><tr><th>Name</th><th>Type</th><th></th><th>Description</th></tr></thead>
        <tbody>${propRows}</tbody>
      </table>
      <h3>Schema (JSON)</h3>
      <pre>${escapeHTML(JSON.stringify(params, null, 2))}</pre>
    `;
  }

  document.getElementById("tool-filter").addEventListener("input", (ev) => {
    renderToolList(ev.target.value);
  });

  // -----------------------------------------------------------------
  // Skills (list + detail)
  // -----------------------------------------------------------------
  async function loadSkills() {
    state.skills = await api("GET", "/api/skills");
    document.getElementById("count-skills").textContent = state.skills.length;
    renderSkillList();
  }

  function renderSkillList(filter) {
    const list = document.getElementById("skill-list");
    list.innerHTML = "";
    const q = (filter || "").toLowerCase();
    const filtered = q
      ? state.skills.filter(s =>
          (s.name || "").toLowerCase().includes(q) ||
          (s.description || "").toLowerCase().includes(q))
      : state.skills;
    if (!filtered.length) {
      const li = document.createElement("li");
      li.className = "empty-row";
      li.textContent = q ? "(no skills match)" : "(no skills discovered)";
      list.appendChild(li);
      return;
    }
    filtered.forEach(s => {
      const li = document.createElement("li");
      if (s.name === state.activeSkill) li.classList.add("active");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = s.name;
      const desc = document.createElement("span");
      desc.className = "desc";
      desc.textContent = s.description || (s.source ? `from ${s.source}` : "");
      li.appendChild(name);
      li.appendChild(desc);
      li.addEventListener("click", () => selectSkill(s.name));
      list.appendChild(li);
    });
  }

  async function selectSkill(name) {
    state.activeSkill = name;
    renderSkillList(document.getElementById("skill-filter").value);
    const detail = document.getElementById("skill-detail");
    detail.innerHTML = '<div class="placeholder">Loading…</div>';
    let skill;
    try {
      skill = await api("GET", "/api/skills/" + encodeURIComponent(name));
    } catch (err) {
      detail.innerHTML = `<div class="placeholder">Error: ${escapeHTML(err.message)}</div>`;
      return;
    }
    detail.innerHTML = `
      <h2>${escapeHTML(skill.name)}</h2>
      <div class="subtitle">${escapeHTML(skill.description || "")}</div>
      <div class="meta">
        <span class="badge">${escapeHTML(skill.source || "skill")}</span>
      </div>
      <h3>Body</h3>
      <div class="body-text">${escapeHTML(skill.body || "")}</div>
    `;
  }

  document.getElementById("skill-filter").addEventListener("input", (ev) => {
    renderSkillList(ev.target.value);
  });

  // -----------------------------------------------------------------
  // Memory (list + detail)
  // -----------------------------------------------------------------
  async function loadMemory() {
    state.memory = await api("GET", "/api/memory");
    document.getElementById("count-memory").textContent = state.memory.length;
    const c2 = document.getElementById("count-memory-2");
    if (c2) c2.textContent = state.memory.length;
    renderMemoryList();
  }

  function renderMemoryList() {
    const list = document.getElementById("memory-list");
    list.innerHTML = "";
    if (!state.memory.length) {
      const li = document.createElement("li");
      li.className = "empty-row";
      li.textContent = "(no memory entries)";
      list.appendChild(li);
      return;
    }
    state.memory.forEach(m => {
      const li = document.createElement("li");
      if (m.key === state.activeMemoryKey) li.classList.add("active");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = m.key;
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = (m.value || "").slice(0, 24);
      const del = document.createElement("span");
      del.className = "del";
      del.textContent = "×";
      del.title = "Delete";
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm("Delete memory entry '" + m.key + "'?")) return;
        await api("DELETE", "/api/memory/" + encodeURIComponent(m.key));
        if (state.activeMemoryKey === m.key) {
          state.activeMemoryKey = null;
          document.getElementById("memory-detail").innerHTML =
            '<div class="placeholder">Select a memory entry to view its value and tags.</div>';
        }
        await loadMemory();
      });
      li.appendChild(name);
      li.appendChild(meta);
      li.appendChild(del);
      li.addEventListener("click", () => selectMemory(m.key));
      list.appendChild(li);
    });
  }

  function selectMemory(key) {
    state.activeMemoryKey = key;
    renderMemoryList();
    const entry = state.memory.find(m => m.key === key);
    if (!entry) return;
    const detail = document.getElementById("memory-detail");
    detail.innerHTML = `
      <h2>${escapeHTML(entry.key)}</h2>
      <div class="meta">
        ${entry.tags ? `<span class="badge">${escapeHTML(entry.tags)}</span>` : ""}
        <span class="badge">value</span>
      </div>
      <h3>Value</h3>
      <pre>${escapeHTML(entry.value || "")}</pre>
    `;
  }

  document.getElementById("mem-save").addEventListener("click", async () => {
    const k = document.getElementById("mem-key").value.trim();
    const v = document.getElementById("mem-value").value;
    if (!k) return;
    await api("POST", "/api/memory", { key: k, value: v });
    document.getElementById("mem-key").value = "";
    document.getElementById("mem-value").value = "";
    await loadMemory();
    selectMemory(k);
  });

  // -----------------------------------------------------------------
  // Config (JSON editor with tree view)
  // -----------------------------------------------------------------
  let _configMode = "tree";  // "tree" or "raw"

  async function loadConfig() {
    state.config = await api("GET", "/api/config");
    renderConfig();
  }

  function renderConfig() {
    if (_configMode === "tree") {
      _renderConfigTree();
    } else {
      _renderConfigRaw();
    }
  }

  function _renderConfigTree() {
    const el = document.getElementById("config-view");
    el.innerHTML = "";
    el.style.display = "";
    const rawEl = document.getElementById("config-raw-view");
    if (rawEl) rawEl.style.display = "none";
    const tree = document.createElement("div");
    tree.className = "json-tree";
    tree.appendChild(_buildJsonTree(state.config, ""));
    el.appendChild(tree);
    document.getElementById("btn-config-tree").classList.add("active");
    document.getElementById("btn-config-raw").classList.remove("active");
  }

  function _renderConfigRaw() {
    const el = document.getElementById("config-raw-view");
    el.innerHTML = "";
    el.style.display = "";
    const treeEl = document.getElementById("config-view");
    if (treeEl) treeEl.style.display = "none";
    const ta = document.createElement("textarea");
    ta.value = JSON.stringify(state.config, null, 2);
    el.appendChild(ta);
    document.getElementById("btn-config-raw").classList.add("active");
    document.getElementById("btn-config-tree").classList.remove("active");
  }

  function _buildJsonTree(data, path) {
    const ul = document.createElement("ul");

    if (data === null || data === undefined) {
      const li = document.createElement("li");
      const span = document.createElement("span");
      span.className = "json-null";
      span.textContent = "null";
      li.appendChild(span);
      ul.appendChild(li);
      return ul;
    }

    if (typeof data === "boolean") {
      const li = document.createElement("li");
      const span = document.createElement("span");
      span.className = "json-bool";
      span.textContent = String(data);
      li.appendChild(span);
      ul.appendChild(li);
      return ul;
    }

    if (typeof data === "number") {
      const li = document.createElement("li");
      const span = document.createElement("span");
      span.className = "json-number";
      span.textContent = String(data);
      li.appendChild(span);
      ul.appendChild(li);
      return ul;
    }

    if (typeof data === "string") {
      const li = document.createElement("li");
      const input = document.createElement("input");
      input.type = "text";
      input.value = data;
      input.className = "json-value-edit";
      input.dataset.path = path;
      input.addEventListener("change", (e) => {
        _setJsonPath(state.config, path, e.target.value);
        _updateConfigStatus("modified");
      });
      li.appendChild(input);
      ul.appendChild(li);
      return ul;
    }

    if (Array.isArray(data)) {
      const li = document.createElement("li");
      const toggle = document.createElement("span");
      toggle.className = "json-toggle";
      toggle.textContent = "\u25BC";
      li.appendChild(toggle);
      const openBracket = document.createElement("span");
      openBracket.className = "json-bracket";
      openBracket.textContent = "[";
      li.appendChild(openBracket);
      if (data.length === 0) {
        const closeBracket = document.createElement("span");
        closeBracket.className = "json-bracket";
        closeBracket.textContent = "]";
        li.appendChild(closeBracket);
      } else {
        const childContainer = document.createElement("span");
        childContainer.className = "json-children";
        const childUl = document.createElement("ul");
        data.forEach((item, i) => {
          const childLi = document.createElement("li");
          const childPath = path ? path + "[" + i + "]" : "[" + i + "]";
          childLi.appendChild(_buildJsonTree(item, childPath));
          childUl.appendChild(childLi);
        });
        childContainer.appendChild(childUl);
        li.appendChild(childContainer);
        const closeBracket = document.createElement("span");
        closeBracket.className = "json-bracket";
        closeBracket.textContent = "]";
        li.appendChild(closeBracket);
      }
      toggle.addEventListener("click", () => {
        const children = li.querySelector(".json-children");
        if (children) {
          children.classList.toggle("collapsed");
          toggle.classList.toggle("collapsed");
        }
      });
      ul.appendChild(li);
      return ul;
    }

    if (typeof data === "object") {
      const keys = Object.keys(data);
      const li = document.createElement("li");
      const toggle = document.createElement("span");
      toggle.className = "json-toggle";
      toggle.textContent = "\u25BC";
      li.appendChild(toggle);
      const openBracket = document.createElement("span");
      openBracket.className = "json-bracket";
      openBracket.textContent = "{";
      li.appendChild(openBracket);
      if (keys.length === 0) {
        const closeBracket = document.createElement("span");
        closeBracket.className = "json-bracket";
        closeBracket.textContent = "}";
        li.appendChild(closeBracket);
      } else {
        const childContainer = document.createElement("span");
        childContainer.className = "json-children";
        const childUl = document.createElement("ul");
        keys.forEach((key) => {
          const childLi = document.createElement("li");
          const keySpan = document.createElement("span");
          keySpan.className = "json-key";
          keySpan.textContent = '"' + key + '": ';
          childLi.appendChild(keySpan);
          const childPath = path ? path + "." + key : key;
          const valueTree = _buildJsonTree(data[key], childPath);
          // Append the value's children to the same li.
          if (valueTree.firstChild) {
            while (valueTree.firstChild) {
              childLi.appendChild(valueTree.firstChild);
            }
          }
          childUl.appendChild(childLi);
        });
        childContainer.appendChild(childUl);
        li.appendChild(childContainer);
        const closeBracket = document.createElement("span");
        closeBracket.className = "json-bracket";
        closeBracket.textContent = "}";
        li.appendChild(closeBracket);
      }
      toggle.addEventListener("click", () => {
        const children = li.querySelector(".json-children");
        if (children) {
          children.classList.toggle("collapsed");
          toggle.classList.toggle("collapsed");
        }
      });
      ul.appendChild(li);
      return ul;
    }

    return ul;
  }

  function _setJsonPath(obj, path, value) {
    if (!path) return;
    const parts = path.replace(/\[(\d+)\]/g, ".$1").split(".");
    let current = obj;
    for (let i = 0; i < parts.length - 1; i++) {
      const key = parts[i];
      if (current[key] === undefined) return;
      current = current[key];
    }
    const lastKey = parts[parts.length - 1];
    current[lastKey] = value;
  }

  function _updateConfigStatus(status) {
    const el = document.getElementById("config-status");
    if (el) el.textContent = status;
  }

  document.getElementById("btn-config-tree").addEventListener("click", () => {
    _configMode = "tree";
    renderConfig();
  });

  document.getElementById("btn-config-raw").addEventListener("click", () => {
    _configMode = "raw";
    renderConfig();
  });

  document.getElementById("btn-config-save").addEventListener("click", async () => {
    let configToSave;
    if (_configMode === "raw") {
      const ta = document.querySelector("#config-raw-view textarea");
      try {
        configToSave = JSON.parse(ta.value);
      } catch (err) {
        alert("Invalid JSON: " + err.message);
        return;
      }
    } else {
      configToSave = state.config;
    }
    try {
      await api("PUT", "/api/config", configToSave);
      state.config = configToSave;
      _updateConfigStatus("saved");
      flash("btn-config-save", "Saved");
      await loadStatus();
    } catch (err) {
      alert("Save failed: " + err.message);
    }
  });

  document.getElementById("btn-config-reload").addEventListener("click", async () => {
    await loadConfig();
    _updateConfigStatus("");
    flash("btn-config-reload", "Reloaded");
    await loadStatus();
  });

  function flash(btnId, msg) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    const original = btn.textContent;
    btn.textContent = msg;
    btn.disabled = true;
    setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1200);
  }

  // -----------------------------------------------------------------
  // Environment variables
  // -----------------------------------------------------------------
  async function loadEnv() {
    try {
      const data = await api("GET", "/api/env");
      state.envData = data.vars || {};
      state.envPersistent = data.persistent || [];
      renderEnv();
    } catch (err) {
      console.error("loadEnv:", err);
    }
  }

  function renderEnv() {
    const tbody = document.getElementById("env-tbody");
    const filter = (document.getElementById("env-filter").value || "").toLowerCase();
    const keys = Object.keys(state.envData).sort();
    const filtered = filter ? keys.filter(k => k.toLowerCase().includes(filter)) : keys;

    const countEl = document.getElementById("env-count");
    if (countEl) {
      const total = keys.length;
      const shown = filtered.length;
      countEl.textContent = shown === total ? total + " variables" : shown + " of " + total + " variables";
    }

    tbody.innerHTML = "";
    filtered.forEach(name => {
      const val = state.envData[name];
      const isPersistent = state.envPersistent.includes(name);

      // Row 1: variable name (full width)
      const trName = document.createElement("tr");
      trName.className = "env-row-name";
      const tdName = document.createElement("td");
      tdName.className = "env-cell-name";
      tdName.colSpan = 2;
      const nameSpan = document.createElement("span");
      nameSpan.className = "env-name-text";
      nameSpan.textContent = name;
      tdName.appendChild(nameSpan);
      if (isPersistent) {
        const badge = document.createElement("span");
        badge.className = "env-badge-persistent";
        badge.textContent = "saved";
        badge.title = "Persisted to env.json — survives restart";
        tdName.appendChild(badge);
      }
      trName.appendChild(tdName);
      tbody.appendChild(trName);

      // Row 2: value + actions
      const trVal = document.createElement("tr");
      trVal.className = "env-row-val";

      const tdVal = document.createElement("td");
      tdVal.className = "env-cell-value";
      const valInput = document.createElement("input");
      valInput.type = "text";
      valInput.value = val;
      valInput.className = "env-val-input";
      valInput.readOnly = true;
      valInput.dataset.name = name;
      tdVal.appendChild(valInput);
      trVal.appendChild(tdVal);

      const tdAct = document.createElement("td");
      tdAct.className = "env-cell-actions";

      const editBtn = document.createElement("button");
      editBtn.className = "icon-btn env-edit-btn";
      editBtn.title = "Edit value";
      editBtn.textContent = "✎";
      editBtn.addEventListener("click", () => {
        if (valInput.readOnly) {
          valInput.readOnly = false;
          valInput.focus();
          editBtn.textContent = "✔";
          editBtn.title = "Save";
          editBtn.classList.add("env-editing");
        } else {
          valInput.readOnly = true;
          editBtn.textContent = "✎";
          editBtn.title = "Edit value";
          editBtn.classList.remove("env-editing");
          const newVal = valInput.value;
          const persistent = state.envPersistent.includes(name);
          api("PUT", "/api/env", { name: name, value: newVal, persistent: persistent });
          state.envData[name] = newVal;
        }
      });
      valInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !valInput.readOnly) {
          editBtn.click();
        }
        if (e.key === "Escape") {
          valInput.value = val;
          valInput.readOnly = true;
          editBtn.textContent = "✎";
          editBtn.title = "Edit value";
          editBtn.classList.remove("env-editing");
        }
      });
      tdAct.appendChild(editBtn);

      const delBtn = document.createElement("button");
      delBtn.className = "icon-btn env-del-btn";
      delBtn.title = "Remove";
      delBtn.textContent = "×";
      delBtn.addEventListener("click", async () => {
        if (!confirm("Remove environment variable '" + name + "'?" + (isPersistent ? " This will also delete it from env.json." : ""))) return;
        await api("DELETE", "/api/env", { name: name });
        delete state.envData[name];
        state.envPersistent = state.envPersistent.filter(k => k !== name);
        renderEnv();
      });
      tdAct.appendChild(delBtn);

      trVal.appendChild(tdAct);
      tbody.appendChild(trVal);
    });
  }

  // Search filter
  document.getElementById("env-filter").addEventListener("input", () => { renderEnv(); });

  // Add button → modal
  document.getElementById("btn-env-add").addEventListener("click", () => {
    _showEnvAddModal();
  });

  function _showEnvAddModal() {
    const overlay = document.createElement("div");
    overlay.className = "approval-overlay";
    const dialog = document.createElement("div");
    dialog.className = "approval-dialog";

    const title = document.createElement("h3");
    title.textContent = "Add Environment Variable";
    dialog.appendChild(title);

    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = "VARIABLE_NAME";
    nameInput.className = "sudo-input";
    nameInput.autocomplete = "off";
    dialog.appendChild(nameInput);

    const valInput = document.createElement("input");
    valInput.type = "text";
    valInput.placeholder = "value";
    valInput.className = "sudo-input";
    valInput.style.marginTop = "6px";
    valInput.autocomplete = "off";
    dialog.appendChild(valInput);

    // Persistent checkbox
    const checkWrap = document.createElement("label");
    checkWrap.className = "env-check-wrap";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = false;
    checkWrap.appendChild(checkbox);
    const checkLabel = document.createTextNode(" Persist to env.json (survives restart)");
    checkWrap.appendChild(checkLabel);
    dialog.appendChild(checkWrap);

    const errorMsg = document.createElement("div");
    errorMsg.className = "sudo-error";
    dialog.appendChild(errorMsg);

    const actions = document.createElement("div");
    actions.className = "approval-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "secondary";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", () => { overlay.remove(); });

    const addBtn = document.createElement("button");
    addBtn.className = "primary";
    addBtn.textContent = "Add";
    addBtn.addEventListener("click", async () => {
      const name = nameInput.value.trim();
      const value = valInput.value;
      if (!name) { errorMsg.textContent = "Name is required"; return; }
      if (state.envData[name]) { errorMsg.textContent = "Variable already exists — edit it instead"; return; }
      try {
        await api("PUT", "/api/env", { name: name, value: value, persistent: checkbox.checked });
        state.envData[name] = value;
        if (checkbox.checked) state.envPersistent.push(name);
        overlay.remove();
        renderEnv();
      } catch (err) {
        errorMsg.textContent = err.message;
      }
    });

    actions.appendChild(cancelBtn);
    actions.appendChild(addBtn);
    dialog.appendChild(actions);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    nameInput.focus();
  }

  // -----------------------------------------------------------------
  // Cron jobs
  // -----------------------------------------------------------------
  let _cronPollTimer = null;
  let _cronPollSeenRunning = false;
  let _cronPollOrigLastRun = null;

  function _pollCronUntilDone(jobId) {
    if (_cronPollTimer) clearInterval(_cronPollTimer);
    _cronPollSeenRunning = false;
    const job = state.cronJobs.find(j => j.id === jobId);
    _cronPollOrigLastRun = job?.last_run_at || null;

    _cronPollTimer = setInterval(async () => {
      try {
        const data = await api("GET", "/api/cron");
        const jobs = data.jobs || [];
        state.cronJobs = jobs;
        const job = jobs.find(j => j.id === jobId);
        if (!job) { clearInterval(_cronPollTimer); _cronPollTimer = null; return; }

        if (job.state === "running") {
          _cronPollSeenRunning = true;
          return; // still running, keep polling
        }

        // Job is no longer running.
        if (_cronPollSeenRunning) {
          // We saw it running before → it finished. Stop polling.
          clearInterval(_cronPollTimer);
          _cronPollTimer = null;
          renderCronList();
          selectCronJob(jobId);
        } else if (job.last_run_at !== _cronPollOrigLastRun) {
          // last_run_at changed without us seeing "running" (fast job or
          // we missed the running state). Treat as done.
          clearInterval(_cronPollTimer);
          _cronPollTimer = null;
          renderCronList();
          selectCronJob(jobId);
        }
        // else: scheduler hasn't picked up the job yet, keep polling
      } catch (_) {
        // keep polling
      }
    }, 2000);
  }

  async function loadCronJobs() {
    try {
      const data = await api("GET", "/api/cron");
      state.cronJobs = data.jobs || [];
      document.getElementById("count-cron").textContent = state.cronJobs.length;
      const c2 = document.getElementById("count-cron-2");
      if (c2) c2.textContent = state.cronJobs.length;
      renderCronList();
    } catch (err) {
      console.error("loadCronJobs:", err);
    }
  }

  function renderCronList() {
    const list = document.getElementById("cron-list");
    list.innerHTML = "";
    if (!state.cronJobs.length) {
      const li = document.createElement("li");
      li.className = "empty-row";
      li.textContent = "(no cron jobs)";
      list.appendChild(li);
      return;
    }
    state.cronJobs.forEach(j => {
      const li = document.createElement("li");
      if (j.id === state.activeCronJob) li.classList.add("active");
      const dot = document.createElement("span");
      dot.className = "cron-status " + (j.state || "scheduled");
      li.appendChild(dot);
      const info = document.createElement("span");
      info.style.flex = "1";
      info.style.minWidth = "0";
      const nameEl = document.createElement("span");
      nameEl.className = "name";
      nameEl.textContent = j.name || j.id.slice(0, 8);
      info.appendChild(nameEl);
      const schedEl = document.createElement("span");
      schedEl.className = "desc";
      schedEl.textContent = j.schedule_display || j.schedule?.expr || "";
      info.appendChild(schedEl);
      li.appendChild(info);
      const del = document.createElement("span");
      del.className = "del";
      del.textContent = "\u00d7";
      del.title = "Delete job";
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm("Delete cron job '" + (j.name || j.id) + "'?")) return;
        await api("DELETE", "/api/cron/" + j.id);
        if (state.activeCronJob === j.id) {
          state.activeCronJob = null;
          document.getElementById("cron-detail").innerHTML =
            '<div class="placeholder">Select a job to view its details.</div>';
        }
        await loadCronJobs();
      });
      li.appendChild(del);
      li.addEventListener("click", () => selectCronJob(j.id));
      list.appendChild(li);
    });
  }

  function _loadCronRunHistory(jobId) {
    const container = document.getElementById("cron-run-history");
    if (!container) return;
    api("GET", "/api/cron/" + jobId + "/sessions").then(data => {
      const sessions = data.sessions || [];
      if (!sessions.length) {
        container.textContent = "(no runs yet)";
        return;
      }
      container.innerHTML = "";
      sessions.forEach(s => {
        // Skip sessions with no displayable messages.
        if (!s.messages || !s.messages.length) return;

        const card = document.createElement("details");
        card.className = "cron-run-card";

        const summary = document.createElement("summary");
        summary.className = "cron-run-header";
        const timeStr = s.created_at
          ? new Date(s.created_at * 1000).toLocaleString()
          : s.id.split("_").pop();
        summary.textContent = s.title || timeStr;
        card.appendChild(summary);

        const msgList = document.createElement("div");
        msgList.className = "cron-run-messages";
        s.messages.forEach(m => {
          const content = (m.content || "").trim();
          if (!content) return;
          const bubble = document.createElement("div");
          bubble.className = "cron-msg cron-msg-" + m.role;
          const label = document.createElement("span");
          label.className = "cron-msg-role";
          label.textContent = m.role === "user" ? "Prompt" : "Response";
          bubble.appendChild(label);
          const body = document.createElement("div");
          body.className = "cron-msg-body";
          body.innerHTML = _renderCronMarkdown(content);
          bubble.appendChild(body);
          msgList.appendChild(bubble);
        });
        // Only append card if it has messages.
        if (msgList.children.length > 0) {
          card.appendChild(msgList);
          container.appendChild(card);
        }
      });
      if (!container.children.length) {
        container.textContent = "(no runs yet)";
      }
    }).catch(() => {
      container.textContent = "(failed to load run history)";
    });
  }

  function _renderCronMarkdown(text) {
    // Minimal markdown: code blocks, inline code, bold, links.
    let s = escapeHTML(text);
    // Fenced code blocks.
    s = s.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // Inline code.
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold.
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Links.
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // Newlines.
    s = s.replace(/\n/g, '<br>');
    return s;
  }

  function selectCronJob(id) {
    state.activeCronJob = id;
    renderCronList();
    const job = state.cronJobs.find(j => j.id === id);
    if (!job) return;
    const detail = document.getElementById("cron-detail");
    const schedDisplay = job.schedule_display || (job.schedule && job.schedule.expr) || "";
    const nextRun = job.next_run_at ? new Date(job.next_run_at).toLocaleString() : "—";
    const lastRun = job.last_run_at ? new Date(job.last_run_at).toLocaleString() : "—";
    const statusClass = job.state || "scheduled";
    const statusLabel = statusClass.charAt(0).toUpperCase() + statusClass.slice(1);

    detail.innerHTML = `
      <h2>${escapeHTML(job.name || job.id)}</h2>
      <div class="cron-detail-meta">
        <span class="badge">${escapeHTML(job.id)}</span>
        <span class="cron-status ${statusClass}" style="width:10px;height:10px"></span>
        <span>${statusLabel}</span>
        <span>Schedule: <code>${escapeHTML(schedDisplay)}</code></span>
      </div>
      <div class="cron-detail-actions">
        <button id="cron-btn-run" class="primary${job.state === "running" ? " running" : ""}"${job.state === "running" ? " disabled" : ""}>${job.state === "running" ? "⏳ Running…" : "▶ Run Now"}</button>
        ${job.enabled && job.state !== "paused"
          ? '<button id="cron-btn-pause" class="secondary">⏸ Pause</button>'
          : '<button id="cron-btn-resume" class="secondary">▶ Resume</button>'}
        <button id="cron-btn-delete" class="secondary" style="color:var(--danger)">🗑 Delete</button>
      </div>
      <h3>Schedule</h3>
      <div style="font-size:13px;margin-bottom:12px">
        <div>Type: <strong>${escapeHTML(job.schedule?.kind || "?")}</strong></div>
        <div>Expression: <code>${escapeHTML(schedDisplay)}</code></div>
        <div>Next run: ${nextRun}</div>
        <div>Last run: ${lastRun}</div>
        ${job.last_status ? `<div>Last status: <strong>${escapeHTML(job.last_status)}</strong></div>` : ""}
        ${job.last_error ? `<div style="color:var(--danger)">Error: ${escapeHTML(job.last_error)}</div>` : ""}
        ${job.command ? `<div>Command: <code>${escapeHTML(job.command)}</code></div>` : ""}
        ${job.prompt ? `<div>Prompt: ${escapeHTML(job.prompt.slice(0, 200))}${job.prompt.length > 200 ? "…" : ""}</div>` : ""}
        ${job.model ? `<div>Model: <code>${escapeHTML(job.model)}</code></div>` : ""}
        ${job.workdir ? `<div>Workdir: <code>${escapeHTML(job.workdir)}</code></div>` : ""}
      </div>
      <h3>Recent Output</h3>
      <div id="cron-output" class="cron-output">Loading…</div>
      <h3>Run History</h3>
      <div id="cron-run-history" class="cron-run-history">Loading…</div>
    `;

    // Load output.
    api("GET", "/api/cron/" + id + "/output").then(data => {
      const out = document.getElementById("cron-output");
      if (out) out.textContent = data.output || "(no output)";
    }).catch(() => {
      const out = document.getElementById("cron-output");
      if (out) out.textContent = "(failed to load output)";
    });

    // Load run history (sessions for this cron job).
    _loadCronRunHistory(id);

    // Wire up action buttons.
    document.getElementById("cron-btn-run")?.addEventListener("click", async () => {
      const btn = document.getElementById("cron-btn-run");
      btn.textContent = "⏳ Running…";
      btn.classList.add("running");
      btn.disabled = true;
      await api("POST", "/api/cron/" + id + "/run");
      // Poll until the job is no longer running, then refresh.
      _pollCronUntilDone(id);
    });
    document.getElementById("cron-btn-pause")?.addEventListener("click", async () => {
      await api("POST", "/api/cron/" + id + "/pause");
      await loadCronJobs();
      selectCronJob(id);
    });
    document.getElementById("cron-btn-resume")?.addEventListener("click", async () => {
      await api("POST", "/api/cron/" + id + "/resume");
      await loadCronJobs();
      selectCronJob(id);
    });
    document.getElementById("cron-btn-delete")?.addEventListener("click", async () => {
      if (!confirm("Delete cron job '" + (job.name || job.id) + "'?")) return;
      await api("DELETE", "/api/cron/" + id);
      state.activeCronJob = null;
      document.getElementById("cron-detail").innerHTML =
        '<div class="placeholder">Select a job to view its details.</div>';
      await loadCronJobs();
    });
  }

  document.getElementById("btn-cron-new").addEventListener("click", () => {
    _showCronNewModal();
  });

  function _showCronNewModal() {
    const overlay = document.createElement("div");
    overlay.className = "approval-overlay";
    const dialog = document.createElement("div");
    dialog.className = "approval-dialog";
    dialog.style.maxWidth = "560px";

    const title = document.createElement("h3");
    title.textContent = "Create Cron Job";
    dialog.appendChild(title);

    const form = document.createElement("div");
    form.className = "cron-form";

    // Name
    const nameLabel = document.createElement("label");
    nameLabel.textContent = "Name";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = "e.g. daily-backup";
    form.appendChild(nameLabel);
    form.appendChild(nameInput);

    // Schedule
    const schedLabel = document.createElement("label");
    schedLabel.textContent = "Schedule";
    const schedInput = document.createElement("input");
    schedInput.type = "text";
    schedInput.placeholder = "0 9 * * *  |  every 2h  |  30m  |  2026-12-01T10:00";
    form.appendChild(schedLabel);
    form.appendChild(schedInput);
    const schedHint = document.createElement("div");
    schedHint.className = "form-hint";
    schedHint.textContent = "Cron expression, interval (every 2h), duration (30m), or ISO timestamp";
    form.appendChild(schedHint);

    // Mode toggle
    const modeRow = document.createElement("div");
    modeRow.className = "form-row";
    const modeLabel = document.createElement("label");
    modeLabel.textContent = "Mode";
    const modeSelect = document.createElement("select");
    modeSelect.innerHTML = '<option value="command">Shell Command</option><option value="prompt">Agent Prompt</option>';
    modeRow.appendChild(modeLabel);
    modeRow.appendChild(modeSelect);
    form.appendChild(modeRow);

    // Command
    const cmdLabel = document.createElement("label");
    cmdLabel.textContent = "Shell Command";
    const cmdInput = document.createElement("input");
    cmdInput.type = "text";
    cmdInput.placeholder = "echo hello && ls -la";
    form.appendChild(cmdLabel);
    form.appendChild(cmdInput);

    // Prompt
    const promptLabel = document.createElement("label");
    promptLabel.textContent = "Agent Prompt";
    promptLabel.style.display = "none";
    const promptInput = document.createElement("textarea");
    promptInput.placeholder = "What should the agent do?";
    promptInput.style.display = "none";
    form.appendChild(promptLabel);
    form.appendChild(promptInput);

    modeSelect.addEventListener("change", () => {
      if (modeSelect.value === "prompt") {
        cmdLabel.style.display = "none";
        cmdInput.style.display = "none";
        promptLabel.style.display = "";
        promptInput.style.display = "";
      } else {
        cmdLabel.style.display = "";
        cmdInput.style.display = "";
        promptLabel.style.display = "none";
        promptInput.style.display = "none";
      }
    });

    // Model override
    const modelRow = document.createElement("div");
    modelRow.className = "form-row";
    const modelLabel = document.createElement("label");
    modelLabel.textContent = "Model Override (optional)";
    const modelInput = document.createElement("input");
    modelInput.type = "text";
    modelInput.placeholder = "leave empty for default";
    modelRow.appendChild(modelLabel);
    modelRow.appendChild(modelInput);
    form.appendChild(modelRow);

    // Workdir
    const workdirLabel = document.createElement("label");
    workdirLabel.textContent = "Working Directory (optional)";
    const workdirInput = document.createElement("input");
    workdirInput.type = "text";
    workdirInput.placeholder = "/path/to/project";
    form.appendChild(workdirLabel);
    form.appendChild(workdirInput);

    // Error
    const errorMsg = document.createElement("div");
    errorMsg.className = "sudo-error";
    form.appendChild(errorMsg);

    // Actions
    const actions = document.createElement("div");
    actions.className = "form-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "secondary";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", () => overlay.remove());

    const createBtn = document.createElement("button");
    createBtn.className = "primary";
    createBtn.textContent = "Create";
    createBtn.addEventListener("click", async () => {
      const name = nameInput.value.trim();
      const schedule = schedInput.value.trim();
      if (!name) { errorMsg.textContent = "Name is required"; return; }
      if (!schedule) { errorMsg.textContent = "Schedule is required"; return; }
      const body = { name, schedule };
      if (modeSelect.value === "prompt") {
        body.prompt = promptInput.value.trim();
        if (!body.prompt) { errorMsg.textContent = "Prompt is required"; return; }
      } else {
        body.command = cmdInput.value.trim();
        if (!body.command) { errorMsg.textContent = "Command is required"; return; }
      }
      if (modelInput.value.trim()) body.model = modelInput.value.trim();
      if (workdirInput.value.trim()) body.workdir = workdirInput.value.trim();
      try {
        const res = await api("POST", "/api/cron", body);
        overlay.remove();
        await loadCronJobs();
        selectCronJob(res.job.id);
      } catch (err) {
        errorMsg.textContent = err.message;
      }
    });

    actions.appendChild(cancelBtn);
    actions.appendChild(createBtn);
    form.appendChild(actions);

    dialog.appendChild(form);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    nameInput.focus();
  }

  // -----------------------------------------------------------------
  // Status / model picker
  // -----------------------------------------------------------------
  async function loadStatus() {
    state.status = await api("GET", "/api/status");
    if (state.status.auth_token) {
      state.token = state.status.auth_token;
      try { localStorage.setItem("hermeslite.token", state.token); } catch (_) {}
    } else {
      state.token = "";
      try { localStorage.removeItem("hermeslite.token"); } catch (_) {}
    }
    const sel = document.getElementById("provider-select");
    sel.innerHTML = "";
    (state.status.providers || []).forEach(p => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      if (p === state.status.provider) opt.selected = true;
      sel.appendChild(opt);
    });
    document.getElementById("model-input").value = state.status.model || "";
    const v = document.getElementById("version-badge");
    if (v) v.textContent = "v" + (state.status.version || "?");
  }

  document.getElementById("provider-select").addEventListener("change", async (ev) => {
    await api("PUT", "/api/config", { model: { provider: ev.target.value } });
    state.status.provider = ev.target.value;
  });

  document.getElementById("model-input").addEventListener("change", async (ev) => {
    await api("PUT", "/api/config", {
      model: { name: ev.target.value, provider: state.status.provider }
    });
    state.status.model = ev.target.value;
  });

  // -----------------------------------------------------------------
  // Profiles (multi-agent)
  // -----------------------------------------------------------------
  async function loadProfiles() {
    try {
      const data = await api("GET", "/api/profiles");
      state.profiles = data.profiles || [];
      state.activeProfile = data.active || "default";
      renderProfileSelect();
    } catch (_) {}
  }

  function renderProfileSelect() {
    const sel = document.getElementById("profile-select");
    sel.innerHTML = "";
    state.profiles.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = p.name + (p.model ? " (" + p.model + ")" : "");
      if (p.name === state.activeProfile) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  document.getElementById("profile-select").addEventListener("change", async (ev) => {
    const name = ev.target.value;
    try {
      await api("PUT", "/api/profiles/active", { name });
      state.activeProfile = name;
      // Reload everything scoped to the new profile.
      await loadStatus();
      await loadSessions();
      state.activeSession = null;
      renderMessagesEmpty();
    } catch (err) {
      alert("Failed to switch profile: " + err.message);
      renderProfileSelect();
    }
  });

  document.getElementById("btn-new-profile").addEventListener("click", async () => {
    const name = prompt("New profile name (lowercase, alphanumeric, hyphens):");
    if (!name) return;
    try {
      await api("POST", "/api/profiles", { name: name.trim() });
      await loadProfiles();
      // Switch to the new profile.
      const sel = document.getElementById("profile-select");
      sel.value = name.trim();
      sel.dispatchEvent(new Event("change"));
    } catch (err) {
      alert("Failed to create profile: " + err.message);
    }
  });

  document.getElementById("btn-del-profile").addEventListener("click", async () => {
    if (state.activeProfile === "default") {
      alert("Cannot delete the default profile.");
      return;
    }
    if (!confirm("Delete profile '" + state.activeProfile + "'? This cannot be undone.")) return;
    try {
      await api("DELETE", "/api/profiles/" + encodeURIComponent(state.activeProfile));
      state.activeProfile = "default";
      await loadProfiles();
      document.getElementById("profile-select").value = "default";
      document.getElementById("profile-select").dispatchEvent(new Event("change"));
    } catch (err) {
      alert("Failed to delete profile: " + err.message);
    }
  });

  // -----------------------------------------------------------------
  // Theme toggle (dark / light)
  // -----------------------------------------------------------------
  function _applyTheme(dark) {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    const btn = document.getElementById("btn-theme");
    if (btn) btn.textContent = dark ? "☀" : "☽";
  }

  function _initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("hermeslite.theme"); } catch (_) {}
    // Default to dark if nothing saved.
    const dark = saved !== "light";
    _applyTheme(dark);
    return dark;
  }

  let _isDark = _initTheme();

  document.getElementById("btn-theme").addEventListener("click", () => {
    _isDark = !_isDark;
    _applyTheme(_isDark);
    try { localStorage.setItem("hermeslite.theme", _isDark ? "dark" : "light"); } catch (_) {}
  });

  // -----------------------------------------------------------------
  // Debug panel
  // -----------------------------------------------------------------
  function _initDebugPanel() {
    const toggle = document.getElementById("debug-toggle");
    const panel = document.getElementById("debug-panel");
    const log = document.getElementById("debug-log");

    toggle.addEventListener("click", () => {
      panel.classList.toggle("hidden");
    });
    document.getElementById("debug-close").addEventListener("click", () => {
      panel.classList.add("hidden");
    });
    document.getElementById("debug-clear").addEventListener("click", () => {
      state.debugLog = [];
      log.innerHTML = "";
      _lastGroupType = null;
      _lastGroupEl = null;
      _lastGroupBody = null;
      _lastGroupCount = 0;
    });
    // Filter checkboxes
    document.querySelectorAll("#debug-filters input[type='checkbox']").forEach(cb => {
      cb.addEventListener("change", () => _filterDebugLog());
    });

    // --- Drag logic ---
    const header = panel.querySelector(".debug-header");
    let dragging = false, startX, startY, startLeft, startTop;
    header.addEventListener("mousedown", (e) => {
      if (e.target.closest(".debug-btn")) return;
      dragging = true;
      // Switch from right-based to left-based positioning for dragging.
      const rect = panel.getBoundingClientRect();
      panel.style.left = rect.left + "px";
      panel.style.top = rect.top + "px";
      panel.style.right = "auto";
      panel.style.transform = "none";
      startX = e.clientX;
      startY = e.clientY;
      startLeft = rect.left;
      startTop = rect.top;
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      let newLeft = startLeft + dx;
      let newTop = startTop + dy;
      // Clamp to viewport.
      const pw = panel.offsetWidth, ph = panel.offsetHeight;
      newLeft = Math.max(0, Math.min(newLeft, window.innerWidth - pw));
      newTop = Math.max(0, Math.min(newTop, window.innerHeight - ph));
      panel.style.left = newLeft + "px";
      panel.style.top = newTop + "px";
    });
    document.addEventListener("mouseup", () => { dragging = false; });
  }

  function _filterDebugLog() {
    const checked = new Set();
    document.querySelectorAll("#debug-filters input:checked").forEach(cb => {
      checked.add(cb.dataset.filter);
    });
    document.querySelectorAll(".debug-entry").forEach(el => {
      el.style.display = checked.has(el.dataset.type) ? "" : "none";
    });
  }

  function _debugTagClass(type) {
    if (type === "llm_request") return "debug-tag-request";
    if (type === "llm_response") return "debug-tag-response";
    if (type === "tool_call") return "debug-tag-tool";
    if (type === "tool_result") return "debug-tag-result";
    if (type === "thinking_content") return "debug-tag-thinking";
    if (type.includes("error")) return "debug-tag-error";
    if (type === "assistant_text_done") return "debug-tag-text";
    return "debug-tag-event";
  }

  // --- JSON tree renderer (foldable, default collapsed) ---
  function _renderJsonTree(value, key, depth) {
    if (value === null) {
      const s = document.createElement("span");
      s.className = "json-null";
      s.textContent = "null";
      return s;
    }
    if (typeof value === "boolean") {
      const s = document.createElement("span");
      s.className = "json-bool";
      s.textContent = String(value);
      return s;
    }
    if (typeof value === "number") {
      const s = document.createElement("span");
      s.className = "json-num";
      s.textContent = String(value);
      return s;
    }
    if (typeof value === "string") {
      const s = document.createElement("span");
      s.className = "json-str";
      // Truncate very long strings but show full on click.
      const display = value.length > 200 ? value.slice(0, 200) + "…" : value;
      s.textContent = '"' + display + '"';
      if (value.length > 200) {
        s.title = value;
        s.style.cursor = "pointer";
        s.addEventListener("click", () => {
          s.textContent = s.textContent.startsWith('"…')
            ? '"' + value + '"'
            : '"' + display + '"';
        });
      }
      return s;
    }
    if (Array.isArray(value)) {
      const det = document.createElement("details");
      det.className = "json-arr";
      if (depth >= 2) det.open = false; else det.open = true;
      const sum = document.createElement("summary");
      const bracket = document.createElement("span");
      bracket.className = "json-bracket";
      bracket.textContent = "[…] (" + value.length + " items)";
      sum.appendChild(bracket);
      det.appendChild(sum);
      if (key != null) {
        const k = document.createElement("span");
        k.className = "json-key";
        k.textContent = key + ": ";
        sum.insertBefore(k, bracket);
      }
      for (let i = 0; i < value.length; i++) {
        const item = document.createElement("div");
        item.appendChild(_renderJsonTree(value[i], i, depth + 1));
        det.appendChild(item);
      }
      return det;
    }
    if (typeof value === "object") {
      const keys = Object.keys(value);
      const det = document.createElement("details");
      det.className = "json-obj";
      if (depth >= 2) det.open = false; else det.open = true;
      const sum = document.createElement("summary");
      const bracket = document.createElement("span");
      bracket.className = "json-bracket";
      bracket.textContent = "{…} (" + keys.length + " keys)";
      sum.appendChild(bracket);
      det.appendChild(sum);
      if (key != null) {
        const k = document.createElement("span");
        k.className = "json-key";
        k.textContent = key + ": ";
        sum.insertBefore(k, bracket);
      }
      for (const mk of keys) {
        const row = document.createElement("div");
        const kSpan = document.createElement("span");
        kSpan.className = "json-key";
        kSpan.textContent = mk + ": ";
        row.appendChild(kSpan);
        row.appendChild(_renderJsonTree(value[mk], null, depth + 1));
        det.appendChild(row);
      }
      return det;
    }
    const s = document.createElement("span");
    s.textContent = String(value);
    return s;
  }

  // --- Debug entry rendering with consecutive-event grouping ---
  // Groups consecutive thinking_content / assistant_text_delta events
  // into a single collapsible <details> block.  Each individual chunk
  // is still visible as a sub-entry when the group is expanded.
  let _lastGroupType = null;   // event type of the current open group
  let _lastGroupEl = null;     // the <details> element of the current group
  let _lastGroupBody = null;   // the body <div> inside the group
  let _lastGroupCount = 0;     // number of chunks in the current group

  // Types that get grouped when consecutive.
  const _GROUPABLE = new Set(["thinking_content", "assistant_text_delta"]);

  function _renderDebugEntry(evt) {
    const log = document.getElementById("debug-log");

    // --- Try to append to the current group ---
    if (_GROUPABLE.has(evt.type) && evt.type === _lastGroupType && _lastGroupBody) {
      _lastGroupCount++;
      // Update the summary count text.
      const countEl = _lastGroupBody.parentElement.querySelector(".debug-group-count");
      if (countEl) countEl.textContent = "\u00d7 " + _lastGroupCount;
      // Append the chunk as a sub-entry inside the group body.
      _lastGroupBody.appendChild(_makeChunkRow(evt));
      log.scrollTop = log.scrollHeight;
      return;
    }

    // --- Start a new entry / group ---
    _lastGroupType = null;
    _lastGroupEl = null;
    _lastGroupBody = null;
    _lastGroupCount = 0;

    const entry = document.createElement("details");
    entry.className = "debug-entry";
    entry.dataset.type = evt.type;

    const summary = document.createElement("summary");
    const tag = document.createElement("span");
    tag.className = "debug-tag " + _debugTagClass(evt.type);
    tag.textContent = evt.type;
    summary.appendChild(tag);

    // Contextual metadata.
    const meta = document.createElement("span");
    meta.className = "debug-meta";
    if (evt.type === "llm_request" && evt.body) {
      meta.textContent = (evt.body.model || "") + "  " + (evt.url || "");
    } else if (evt.type === "llm_response") {
      meta.textContent = (evt.model || "") + "  " + (evt.finish_reason || "");
      if (evt.usage) {
        meta.textContent += "  " + (evt.usage.prompt_tokens || 0) + "\u2192" + (evt.usage.completion_tokens || 0) + " tok";
      }
    } else if (evt.type === "tool_call") {
      meta.textContent = evt.name || "";
    } else if (evt.type === "tool_result") {
      meta.textContent = (evt.ok ? "ok" : "fail") + "  " + ((evt.data || "").slice(0, 60));
    }
    summary.appendChild(meta);

    // For groupable types, show a count badge.
    if (_GROUPABLE.has(evt.type)) {
      const countSpan = document.createElement("span");
      countSpan.className = "debug-meta debug-group-count";
      countSpan.textContent = "\u00d7 1";
      summary.appendChild(countSpan);
    }

    entry.appendChild(summary);

    const body = document.createElement("div");
    body.className = "debug-body";

    if (_GROUPABLE.has(evt.type)) {
      // This is the first event of a new group — add it as a sub-entry.
      body.appendChild(_makeChunkRow(evt));
      _lastGroupType = evt.type;
      _lastGroupEl = entry;
      _lastGroupBody = body;
      _lastGroupCount = 1;
    } else if (evt.type === "assistant_text_done") {
      // Render final text content as plain text.
      body.textContent = evt.text || "";
    } else {
      // Build a foldable JSON tree for everything else.
      body.className = "debug-body json-tree";
      body.appendChild(_renderJsonTree(evt, null, 0));
    }

    entry.appendChild(body);
    log.appendChild(entry);

    // Auto-expand only request/response events; groups are collapsed by default.
    if (evt.type === "llm_request" || evt.type === "llm_response") {
      entry.open = true;
    }
    log.scrollTop = log.scrollHeight;
  }

  /** Create a single chunk row inside a group. */
  function _makeChunkRow(evt) {
    const row = document.createElement("div");
    row.className = "debug-chunk";
    const tag = document.createElement("span");
    tag.className = "debug-tag " + _debugTagClass(evt.type);
    tag.textContent = evt.type;
    row.appendChild(tag);
    const body = document.createElement("span");
    body.className = "debug-chunk-body";
    const text = evt.text || JSON.stringify(evt);
    body.textContent = text.length > 300 ? text.slice(0, 300) + "\u2026" : text;
    if (text.length > 300) {
      body.title = text;
      body.style.cursor = "pointer";
      body.addEventListener("click", () => {
        body.textContent = body.textContent.endsWith("\u2026")
          ? text
          : (text.length > 300 ? text.slice(0, 300) + "\u2026" : text);
      });
    }
    row.appendChild(body);
    return row;
  }

  // -----------------------------------------------------------------
  // CWD (working directory) setting
  // -----------------------------------------------------------------
  async function _loadCwd() {
    try {
      const data = await api("GET", "/api/cwd");
      document.getElementById("cwd-input").value = data.cwd || "";
    } catch (_) {}
  }

  function _initCwdBar() {
    const input = document.getElementById("cwd-input");
    const btnSet = document.getElementById("cwd-set");
    const btnRefresh = document.getElementById("cwd-refresh");
    const status = document.getElementById("cwd-status");

    btnSet.addEventListener("click", async () => {
      const cwd = input.value.trim();
      status.textContent = "";
      status.className = "cwd-status";
      try {
        await api("PUT", "/api/cwd", { cwd: cwd });
        status.textContent = "✓ saved";
        status.className = "cwd-status ok";
        setTimeout(() => { status.textContent = ""; }, 2000);
      } catch (err) {
        status.textContent = err.message || "error";
        status.className = "cwd-status err";
      }
    });
    btnRefresh.addEventListener("click", _loadCwd);
    // Allow Enter key.
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") btnSet.click();
    });
  }

  // -----------------------------------------------------------------
  // Boot
  // -----------------------------------------------------------------
  (async () => {
    try {
      try { state.token = localStorage.getItem("hermeslite.token") || ""; } catch (_) {}
      await loadStatus();
      await loadProfiles();
      await loadSessions();
      await loadTools();
      await loadSkills();
      await loadMemory();
      await loadCronJobs();
      try { state.commands = await api("GET", "/api/commands"); } catch (_) {}
      // Initialize debug panel based on config.
      _initDebugPanel();
      _initCwdBar();
      try {
        const cfg = await api("GET", "/api/config");
        state.debugMode = (cfg.debug || {}).enabled !== false;
        document.getElementById("debug-toggle").style.display = state.debugMode ? "" : "none";
      } catch (_) {}
      await _loadCwd();
    } catch (err) {
      console.error(err);
      alert("Failed to start: " + err.message);
    }
  })();
})();
