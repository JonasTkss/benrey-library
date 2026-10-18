// Admin help widget. Plain DOM, no framework.
// Security: model output is only ever inserted with textContent, never
// innerHTML, so a reply containing HTML or <script> is shown as text.
(function () {
  "use strict";

  const config = document.querySelector('meta[name="assistant-config"]');
  if (!config) return;

  const endpoint = config.dataset.endpoint;
  const csrf = config.dataset.csrf;
  const adminRoot = config.dataset.adminRoot;
  const root = document.createElement("div");
  root.id = "assistant-root";
  document.body.appendChild(root);
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

  const toggle = el("button", "assistant-toggle", "? Help");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", "assistant-panel");

  const panel = el("section", "assistant-panel");
  panel.id = "assistant-panel";
  panel.hidden = true;
  panel.setAttribute("aria-label", "Library help assistant");

  const header = el("div", "assistant-header");
  header.appendChild(el("strong", "", "Library help"));
  const close = el("button", "assistant-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Close help");
  header.appendChild(close);

  const log = el("div", "assistant-log");
  log.setAttribute("aria-live", "polite");
  log.appendChild(
    el("p", "assistant-hint", "Ask what a field means, or about the library: \u201cWho has the highest bill?\u201d, \u201cIs Dune on the shelf?\u201d, \u201cFind member Anna\u201d.")
  );

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

  function setOpen(open) {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) input.focus();
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

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    const question = input.value.trim();
    if (question.length < 3) return;
    addMessage("user", question);
    input.value = "";
    send.disabled = true;
    const pending = addMessage("bot", "Thinking…");

    try {
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({ question: question, model: currentModel(), field: lastField }),
      });
      const data = await response.json().catch(function () { return {}; });
      pending.remove();
      if (!response.ok) {
        addMessage("bot", data.error || "Sorry, something went wrong. Please try again.");
      } else {
        const extra = el("div");
        if (data.results && data.results.length) extra.appendChild(resultsBlock(data));
        extra.appendChild(sourcesBlock(data));
        addMessage("bot", data.answer, extra);
      }
    } catch (err) {
      pending.remove();
      addMessage("bot", "Could not reach the assistant. Check your connection; the rest of the admin still works.");
    } finally {
      send.disabled = false;
    }
  });
})();
