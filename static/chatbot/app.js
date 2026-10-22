/* Dairy Logistics AI — chat client.
 * All server/user content is inserted with textContent (never innerHTML) to
 * prevent XSS from questions, generated SQL or database values.
 */
(function () {
  "use strict";

  const STREAM_URL = "/chatbot/api/query/stream/";
  const RESET_URL = "/chatbot/api/conversation/reset/";
  const STAGES = [
    ["retrieving", "Finding relevant tables"],
    ["generating", "Writing SQL"],
    ["validating", "Checking the query is safe"],
    ["executing", "Running the query"],
  ];

  const $ = (sel) => document.querySelector(sel);
  const messages = $("#messages");
  const scroller = $("#main");
  const form = $("#composer");
  const input = $("#question");
  const sendBtn = $("#send");
  const charCount = $("#charCount");
  const toastEl = $("#toast");
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  let busy = false;

  // ---------- DOM helpers ----------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of [].concat(children || [])) {
      if (child) node.append(child);
    }
    return node;
  }

  function scrollToBottom() {
    scroller.scrollTop = scroller.scrollHeight;
  }

  let toastTimer;
  function toast(text) {
    toastEl.textContent = text;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toastEl.hidden = true), 2200);
  }

  async function copyText(text, label) {
    try {
      await navigator.clipboard.writeText(text);
      toast(label + " copied");
    } catch (e) {
      toast("Copy failed — your browser blocked clipboard access");
    }
  }

  function download(filename, content, type) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---------- Export formats ----------

  function csvCell(value) {
    if (value === null || value === undefined) return "";
    let text = String(value);
    // Neutralise spreadsheet formula injection.
    if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
    return /[",\n\r]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }

  function toCSV(columns, rows) {
    return [columns, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n");
  }

  function toTSV(columns, rows) {
    const clean = (v) => (v === null || v === undefined ? "" : String(v).replace(/[\t\n\r]+/g, " "));
    return [columns, ...rows].map((r) => r.map(clean).join("\t")).join("\n");
  }

  function stamp() {
    return new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  }

  // ---------- Results table ----------

  function isNumericColumn(rows, index) {
    let seen = false;
    for (const row of rows) {
      const v = row[index];
      if (v === null || v === "") continue;
      if (typeof v === "number" || (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v))) seen = true;
      else return false;
    }
    return seen;
  }

  function renderTable(columns, rows) {
    if (!rows.length) {
      return el("div", { class: "empty-result", text: "The query ran successfully but returned no rows." });
    }
    const numeric = columns.map((_, i) => isNumericColumn(rows, i));
    const data = rows.slice();
    const tbody = el("tbody");
    const headers = [];

    function fill() {
      const frag = document.createDocumentFragment();
      for (const row of data) {
        const tr = el("tr");
        row.forEach((value, i) => {
          const isNull = value === null || value === undefined;
          const text = isNull ? "NULL" : String(value);
          const cls = [numeric[i] ? "num" : "", isNull ? "null" : "", text.length > 60 ? "long" : ""].join(" ").trim();
          tr.append(el("td", { class: cls || null, title: text.length > 60 ? text : null, text }));
        });
        frag.append(tr);
      }
      tbody.replaceChildren(frag);
    }

    function sortBy(index) {
      const th = headers[index];
      const dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
      headers.forEach((h) => h.removeAttribute("aria-sort"));
      th.setAttribute("aria-sort", dir);
      const sign = dir === "ascending" ? 1 : -1;
      data.sort((a, b) => {
        const x = a[index], y = b[index];
        if (x === null) return 1;
        if (y === null) return -1;
        if (numeric[index]) return (Number(x) - Number(y)) * sign;
        return String(x).localeCompare(String(y), undefined, { numeric: true }) * sign;
      });
      fill();
    }

    const headRow = el("tr");
    columns.forEach((name, i) => {
      const th = el("th", { scope: "col", class: numeric[i] ? "num" : null }, [
        el("button", { type: "button", title: "Sort by " + name, text: name, onclick: () => sortBy(i) }),
      ]);
      headers.push(th);
      headRow.append(th);
    });
    fill();
    return el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Query results" }, [
      el("table", { class: "results" }, [el("thead", {}, headRow), tbody]),
    ]);
  }

  // ---------- Messages ----------

  function hideEmptyState() {
    const empty = $("#emptyState");
    if (empty) empty.remove();
  }

  function addUserMessage(text) {
    hideEmptyState();
    messages.append(el("div", { class: "msg msg-user" }, el("div", { class: "bubble", text })));
    scrollToBottom();
  }

  function addProgress() {
    const items = STAGES.map(([key, label]) =>
      el("li", { "data-stage": key }, [el("span", { class: "dot", "aria-hidden": "true" }), el("span", { text: label })])
    );
    const list = el("ol", { class: "progress", "aria-label": "Progress" }, items);
    const wrap = el("div", { class: "msg msg-assistant", "aria-busy": "true" }, el("div", { class: "card" }, list));
    messages.append(wrap);
    scrollToBottom();
    return {
      node: wrap,
      set(stage) {
        let reached = false;
        for (const li of items) {
          li.classList.remove("active");
          if (li.dataset.stage === stage) {
            li.classList.add("active");
            reached = true;
          } else if (!reached) {
            li.classList.add("done");
          }
        }
      },
    };
  }

  function pill(label, value) {
    return el("span", { class: "pill" }, [label + " ", el("strong", { text: value })]);
  }

  function formatMs(ms) {
    if (ms === undefined || ms === null) return "–";
    return ms >= 1000 ? (ms / 1000).toFixed(2) + " s" : Math.round(ms) + " ms";
  }

  function renderResult(data) {
    const { columns, rows } = data;
    const tablePanel = el("div", { role: "tabpanel", class: "panel" }, renderTable(columns, rows));
    const sqlPanel = el("div", { role: "tabpanel", class: "panel", hidden: true }, el("pre", { class: "sql" }, el("code", { text: data.sql })));

    const tabResults = el("button", { type: "button", class: "tab", role: "tab", "aria-selected": "true", text: "Results" });
    const tabSql = el("button", { type: "button", class: "tab", role: "tab", "aria-selected": "false", text: "SQL" });
    function select(showSql) {
      tabResults.setAttribute("aria-selected", String(!showSql));
      tabSql.setAttribute("aria-selected", String(showSql));
      tablePanel.hidden = showSql;
      sqlPanel.hidden = !showSql;
    }
    tabResults.addEventListener("click", () => select(false));
    tabSql.addEventListener("click", () => select(true));
    const tablist = el("div", { class: "tabs", role: "tablist", "aria-label": "Result view" }, [tabResults, tabSql]);

    const hasRows = rows.length > 0;
    const actions = el("div", { class: "actions" }, [
      el("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Copy SQL", onclick: () => copyText(data.sql, "SQL") }),
      hasRows && el("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Copy table", onclick: () => copyText(toTSV(columns, rows), "Table") }),
      hasRows && el("button", {
        type: "button", class: "btn btn-ghost btn-sm", text: "CSV",
        "aria-label": "Export results as CSV",
        onclick: () => download(`results-${stamp()}.csv`, "﻿" + toCSV(columns, rows), "text/csv;charset=utf-8"),
      }),
      hasRows && el("button", {
        type: "button", class: "btn btn-ghost btn-sm", text: "JSON",
        "aria-label": "Export results as JSON",
        onclick: () => {
          const records = rows.map((r) => Object.fromEntries(columns.map((c, i) => [c, r[i]])));
          download(`results-${stamp()}.json`, JSON.stringify(records, null, 2), "application/json");
        },
      }),
    ]);

    const t = data.timings || {};
    const meta = el("div", { class: "meta" }, [
      pill("Rows", String(data.row_count)),
      pill("Query", formatMs(data.execution_ms)),
      pill("Total", formatMs(t.total_ms)),
      data.tables && data.tables.length ? pill("Tables", data.tables.join(", ")) : null,
    ]);

    const card = el("div", { class: "card" }, [
      el("div", { class: "card-head" }, [el("div", {}, [el("p", { class: "summary", text: data.summary }), meta]), actions]),
      data.truncated
        ? el("p", { class: "notice notice-warn", text: `Only the first ${data.max_rows} rows are shown. Add filters to narrow the result.` })
        : null,
      tablist,
      tablePanel,
      sqlPanel,
    ]);
    return el("div", { class: "msg msg-assistant" }, card);
  }

  function renderError(message, sql, retry) {
    const card = el("div", { class: "card error-card", role: "alert" }, [
      el("p", { class: "summary", text: message }),
      sql
        ? el("details", { class: "blocked" }, [el("summary", { text: "Show generated SQL" }), el("pre", { class: "sql" }, el("code", { text: sql }))])
        : null,
      retry ? el("div", { class: "actions retry" }, el("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Try again", onclick: retry })) : null,
    ]);
    return el("div", { class: "msg msg-assistant" }, card);
  }

  // ---------- Networking ----------

  async function ask(question) {
    if (busy) return;
    busy = true;
    sendBtn.disabled = true;
    addUserMessage(question);
    const progress = addProgress();
    progress.set("retrieving");

    let finalNode = null;
    try {
      const res = await fetch(STREAM_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        body: JSON.stringify({ question }),
      });

      if (!res.ok || !res.body) {
        let payload = {};
        try { payload = await res.json(); } catch (e) { /* non-JSON error page */ }
        if (res.status === 401) {
          finalNode = renderError("Your session has expired. Redirecting to sign in…");
          setTimeout(() => (window.location.href = "/accounts/login/?next=/"), 1200);
          return;
        }
        throw Object.assign(new Error(payload.error || `Request failed (${res.status})`), { sql: payload.sql, retryable: res.status >= 500 || res.status === 429 });
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (value) buffer += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, nl).trim();
          buffer = buffer.slice(nl + 1);
          if (!line) continue;
          const event = JSON.parse(line);
          if (event.event === "stage") progress.set(event.stage);
          else if (event.event === "result") finalNode = renderResult(event.data);
          else if (event.event === "error") {
            finalNode = renderError(event.error, event.sql, event.status >= 500 ? () => ask(question) : null);
          }
        }
        if (done) break;
      }
      if (!finalNode) throw Object.assign(new Error("The response ended unexpectedly."), { retryable: true });
    } catch (err) {
      const offline = err instanceof TypeError;
      finalNode = renderError(
        offline ? "Couldn't reach the server. Check your connection and try again." : err.message,
        err.sql,
        offline || err.retryable ? () => ask(question) : null
      );
    } finally {
      progress.node.replaceWith(finalNode || renderError("Something went wrong."));
      busy = false;
      sendBtn.disabled = !input.value.trim();
      scrollToBottom();
      input.focus();
    }
  }

  // ---------- Composer ----------

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
    charCount.textContent = String(input.value.length);
    sendBtn.disabled = busy || !input.value.trim();
  }

  function submit() {
    const question = input.value.trim();
    if (!question || busy) return;
    input.value = "";
    autosize();
    ask(question);
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });

  document.querySelectorAll("[data-example]").forEach((chip) =>
    chip.addEventListener("click", () => {
      if (input.disabled) return;
      input.value = chip.textContent.trim();
      autosize();
      submit();
    })
  );

  $("#newChat").addEventListener("click", async () => {
    try {
      await fetch(RESET_URL, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": csrfToken } });
    } catch (e) { /* history reset is best effort */ }
    window.location.reload();
  });

  autosize();
  if (!input.disabled) input.focus();
})();
