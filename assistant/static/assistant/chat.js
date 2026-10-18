// "Ask AI" widget. Plain DOM, no framework.
// Security: model output is only ever inserted with textContent, never
// innerHTML, so a reply containing HTML or <script> is shown as text.
// The conversation lives in the server-side login session; the browser only
// remembers whether the panel was open and which question is still waiting.
(function () {
  "use strict";

  const config = document.querySelector('meta[name="assistant-config"]');
  if (!config) return;

  const endpoint = config.dataset.endpoint;
  const historyUrl = config.dataset.history;
  const clearUrl = config.dataset.clear;
  const csrf = config.dataset.csrf;
  const adminRoot = config.dataset.adminRoot;
  const PENDING_KEY = "assistant.pending";
  const OPEN_KEY = "assistant.open";
  const POLL_MS = 2000;
  const POLL_LIMIT_MS = 35000; // the server gives up on the AI after ~20s

  // sessionStorage can be unavailable (privacy modes); the widget still works.
  function store(key, value) {
    try {
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* ignore */ }
  }
  function load(key) {
    try { return JSON.parse(sessionStorage.getItem(key)); } catch (e) { return null; }
  }
  const root = document.createElement("div");
  root.id = "assistant-root";
  document.body.appendChild(root);

  // Form pages have a sticky save bar at the bottom (#submit-row); sit above
  // it so the button never covers Save.
  function clearSaveBar() {
    const bar = document.getElementById("submit-row");
    const sticky = bar && getComputedStyle(bar).position === "sticky";
    root.style.bottom = sticky ? bar.offsetHeight + 16 + "px" : "";
  }
  clearSaveBar();
  window.addEventListener("resize", clearSaveBar);
  let lastField = null;

  // Which model's form is open, from a URL like /<admin>/library/loan/12/change/.
  function currentModel() {
    const path = window.location.pathname;
    if (!path.startsWith(adminRoot)) return null;
    const parts = path.slice(adminRoot.length).split("/");
    return parts[0] === "library" && parts[1] ? parts[1] : null;
  }

  // Remember the last form field the user clicked into (main form only;
  // inline fields are named like "loans-0-book" and are skipped).
  document.addEventListener("focusin", function (event) {
    const name = event.target && event.target.name;
    if (name && /^[a-z_]+$/.test(name) && name !== "csrfmiddlewaretoken") {
      lastField = name;
    }
  });

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const toggle = el("button", "assistant-toggle");
  toggle.append(el("span", "material-symbols-outlined assistant-toggle-icon", "auto_awesome"), "Ask AI");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", "assistant-panel");

  const panel = el("section", "assistant-panel");
  panel.id = "assistant-panel";
  panel.hidden = true;
  panel.setAttribute("aria-label", "Ask AI assistant");

  const header = el("div", "assistant-header");
  header.appendChild(el("strong", "", "Ask AI"));
  const headerActions = el("div", "assistant-header-actions");
  const clearBtn = el("button", "assistant-clear", "Clear");
  clearBtn.type = "button";
  clearBtn.title = "Start a new conversation";
  const close = el("button", "assistant-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Close");
  headerActions.append(clearBtn, close);
  header.appendChild(headerActions);

  const log = el("div", "assistant-log");
  log.setAttribute("aria-live", "polite");
  function showHint() {
    log.replaceChildren(
      el("p", "assistant-hint", "Ask what a field means, or about the library: \u201cWho has the highest bill?\u201d, \u201cIs Dune on the shelf?\u201d, \u201cFind member Anna\u201d.")
    );
  }
  showHint();

  const form = el("form", "assistant-form");
  const input = el("textarea");
  input.rows = 2;
  input.maxLength = 500;
  input.placeholder = "e.g. Who has the highest bill?";
  input.setAttribute("aria-label", "Your question");
  const send = el("button", "assistant-send", "Ask");
  send.type = "submit";
  form.append(input, send);

  panel.append(header, log, form);
  root.append(toggle, panel);

  function setOpen(open, focus) {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    store(OPEN_KEY, open);
    if (open) {
      log.scrollTop = log.scrollHeight;
      if (focus !== false) input.focus();
    }
  }
  toggle.addEventListener("click", function () { setOpen(panel.hidden); });
  // Enter sends, Shift+Enter adds a new line.
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  close.addEventListener("click", function () { setOpen(false); });

  function addMessage(role, text, extra) {
    const box = el("div", "assistant-msg assistant-msg--" + role);
    box.appendChild(el("p", "", text));
    if (extra) box.appendChild(extra);
    log.appendChild(box);
    log.scrollTop = log.scrollHeight;
    return box;
  }

  // Lookup results: rows with a link into the admin. URLs come from the
  // server, but only same-site paths under the admin are turned into links.
  function resultsBlock(data) {
    const list = el("ul", "assistant-results");
    (data.results || []).forEach(function (row) {
      const item = el("li");
      const safe = typeof row.url === "string" && row.url.startsWith(adminRoot);
      const link = el(safe ? "a" : "span", "assistant-result-label", row.label);
      if (safe) link.href = row.url;
      item.append(link, el("span", "assistant-result-detail", row.detail));
      list.appendChild(item);
    });
    return list;
  }

  function sourcesBlock(data) {
    const wrap = el("div", "assistant-meta");
    if (data.lookup) {
      wrap.appendChild(el("span", "", "From the live library data."));
    } else if (data.sources && data.sources.length) {
      wrap.appendChild(el("span", "", "Based on: "));
      data.sources.forEach(function (source) {
        wrap.appendChild(el("span", "assistant-chip", source.label));
      });
    } else {
      wrap.appendChild(el("span", "assistant-warn", "Not grounded in a field definition; double-check with an administrator."));
    }
    if (data.degraded && !data.lookup) {  // a database answer is complete even without AI
      wrap.appendChild(el("span", "assistant-warn", data.disabled ? " Assistant is not configured." : " AI unavailable: showing built-in help."));
    }
    return wrap;
  }

  function addAnswer(data) {
    const extra = el("div");
    if (data.results && data.results.length) extra.appendChild(resultsBlock(data));
    extra.appendChild(sourcesBlock(data));
    return addMessage("bot", data.answer, extra);
  }

  function renderHistory(messages) {
    if (!messages.length) return showHint();
    log.replaceChildren();
    messages.forEach(function (m) {
      if (m.role === "user") addMessage("user", m.text);
      else addAnswer(m);
    });
  }

  async function fetchHistory() {
    const response = await fetch(historyUrl, { credentials: "same-origin" });
    if (!response.ok) throw new Error("history " + response.status);
    return (await response.json()).messages || [];
  }

  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  // A question asked on a previous page may still be running on the server.
  // Its answer is saved to the session when it finishes, so poll for it.
  async function resumePending(messages) {
    const pending = load(PENDING_KEY);
    if (!pending) return;
    const answered = function (list) {
      return list.some(function (m) { return m.role === "bot" && m.client_id === pending.id; });
    };
    if (answered(messages)) { store(PENDING_KEY, null); return; }
    if (Date.now() - pending.at > POLL_LIMIT_MS) { store(PENDING_KEY, null); return offerRetry(pending.question); }

    addMessage("user", pending.question);
    const waiting = addMessage("bot", "Still working on this…");
    send.disabled = true;
    while (Date.now() - pending.at < POLL_LIMIT_MS) {
      await new Promise(function (r) { setTimeout(r, POLL_MS); });
      let latest;
      try { latest = await fetchHistory(); } catch (e) { continue; }
      if (answered(latest)) {
        store(PENDING_KEY, null);
        send.disabled = false;
        return renderHistory(latest);
      }
    }
    waiting.remove();
    store(PENDING_KEY, null);
    send.disabled = false;
    offerRetry(pending.question);
  }

  function offerRetry(question) {
    const retry = el("button", "assistant-retry", "Ask again");
    retry.type = "button";
    retry.addEventListener("click", function () { ask(question); });
    addMessage("bot", "The answer to \u201c" + question + "\u201d did not arrive.", retry);
  }

  async function ask(question) {
    addMessage("user", question);
    send.disabled = true;
    const placeholder = addMessage("bot", "Thinking…");
    const id = newId();
    store(PENDING_KEY, { id: id, question: question, at: Date.now() });

    try {
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({ question: question, model: currentModel(), field: lastField, client_id: id }),
      });
      const data = await response.json().catch(function () { return {}; });
      store(PENDING_KEY, null);
      placeholder.remove();
      if (!response.ok) addMessage("bot", data.error || "Sorry, something went wrong. Please try again.");
      else addAnswer(data);
    } catch (err) {
      // Usually the user navigated away: keep the pending marker so the next
      // page picks up the answer. Otherwise it is a real network problem.
      placeholder.remove();
      addMessage("bot", "Could not reach the assistant. Check your connection; the rest of the admin still works.");
    } finally {
      send.disabled = false;
    }
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    const question = input.value.trim();
    if (question.length < 3 || send.disabled) return;
    input.value = "";
    ask(question);
  });

  clearBtn.addEventListener("click", async function () {
    store(PENDING_KEY, null);
    showHint();
    try {
      await fetch(clearUrl, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": csrf } });
    } catch (e) { /* the next page will show the old history; harmless */ }
    input.focus();
  });

  // Restore the conversation and the panel state from the previous page.
  (async function restore() {
    let messages = [];
    try { messages = await fetchHistory(); renderHistory(messages); } catch (e) { /* show the hint */ }
    if (load(OPEN_KEY) || load(PENDING_KEY)) setOpen(true, false);
    resumePending(messages);
  })();
})();
