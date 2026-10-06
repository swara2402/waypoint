/* ============================================================
   WayPoint — Incident Command Console
   Vanilla JS single-page app, same-origin calls to FastAPI.
   ============================================================ */
"use strict";

const BASE_API_URL = (window.PRISM_API_URL || "").replace(/\/$/, "");

/* ---------- Tiny DOM / util helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

const trunc = (s, n = 12) => (s && s.length > n ? s.slice(0, n) + "…" : s || "—");

const pct = (x) => Math.round((Number(x) || 0) * 100);

const fmtDate = (iso) => {
  if (!iso) return "—";
  const t = new Date(iso);
  if (isNaN(t)) return "—";
  return t.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
};

const relTime = (iso) => {
  if (!iso) return "—";
  const t = new Date(iso);
  if (isNaN(t)) return "—";
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 45) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
};

const clock = () =>
  new Date().toLocaleTimeString("en-GB", { hour12: false });

const bar = (x, cls = "") =>
  `<div class="bar-track"><div class="bar-fill ${cls}" style="width:${Math.min(100, Math.max(0, pct(x)))}%"></div></div>`;

const confCls = (c) => (c >= 0.65 ? "ok" : c >= 0.4 ? "warn" : "bad");

/* ---------- API layer ---------- */
function getApiKey() {
  return localStorage.getItem("prism_api_key") || "";
}

function setApiKey(key) {
  if (key) localStorage.setItem("prism_api_key", key);
  else localStorage.removeItem("prism_api_key");
}

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: { "X-Request-ID": crypto.randomUUID?.() || String(Date.now()) } };
  const key = getApiKey();
  if (key) opts.headers["X-API-Key"] = key;
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(BASE_API_URL + path, opts);
  } catch (_e) {
    const err = new Error("Network error — backend unreachable");
    err.status = 0;
    throw err;
  }
  let data = null;
  try { data = await res.json(); }
  catch (_e) { data = await res.text().catch(() => null); }
  if (!res.ok) {
    let msg = data?.detail ?? data?.message ?? (res.statusText || `HTTP ${res.status}`);
    if (typeof msg !== "string") msg = JSON.stringify(msg);
    if (res.status === 401) msg = "Session is not authorized for this request";
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ---------- Toast ---------- */
function toast(msg, { type = "info", title } = {}) {
  const wrap = $("#toasts");
  const icons = {
    success: "i-check", error: "i-alert", info: "i-activity", warn: "i-alert",
  };
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.innerHTML = `
    <svg class="ic"><use href="#${icons[type] || "i-activity"}"/></svg>
    <div class="toast-body">
      ${title ? `<b>${esc(title)}</b>` : ""}
      <span>${esc(msg)}</span>
    </div>
    <button class="toast-close" aria-label="Dismiss"><svg class="ic"><use href="#i-close"/></svg></button>
    <div class="toast-progress" style="color:var(--accent)"></div>`;
  $(".toast-progress", el).style.animationDuration = "4.5s";
  $(".toast-close", el).onclick = () => dismiss();
  const dismiss = () => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  };
  setTimeout(dismiss, 4800);
  wrap.appendChild(el);
}

/* ---------- Copy affordance (event delegation) ---------- */
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const val = btn.dataset.copy ?? "";
  try {
    await navigator.clipboard.writeText(val);
    const holder = btn.closest(".mono-id");
    if (holder) {
      holder.classList.add("copied");
      setTimeout(() => holder.classList.remove("copied"), 1100);
    }
  } catch (_e) { /* clipboard blocked — ignore */ }
});

const monoId = (id) =>
  `<span class="mono-id" title="${esc(id)}"><span>${esc(trunc(id))}</span>
   <button class="copy-btn" data-copy="${esc(id)}" aria-label="Copy"><svg class="ic"><use href="#i-copy"/></svg></button></span>`;

/* ---------- Pills ---------- */
const sevPill = (sev) => `<span class="pill sev-${esc(sev || "P4")}">${esc(sev || "—")}</span>`;

const statusPill = (status) => {
  const s = (status || "").toLowerCase();
  let cls = "pill-dim";
  if (s === "resolved") cls = "pill-ok";
  else if (s === "investigating") cls = "pill-warn";
  else if (s === "active" || s === "open") cls = "pill-acc";
  return `<span class="pill ${cls}">${esc(status || "—")}</span>`;
};

const chips = (arr) =>
  arr && arr.length
    ? `<span class="chips">${arr.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</span>`
    : `<span class="tbl-sub">—</span>`;

/* ---------- Skeleton / empty / error states ---------- */
const skeletonRows = (n = 5) =>
  `<div class="skeleton-rows">${Array(n).fill('<div class="skeleton"></div>').join("")}</div>`;

const emptyState = (title, body, action) => `
  <div class="empty-state">
    <svg class="ic"><use href="#i-radar"/></svg>
    <p class="empty-title">${esc(title)}</p>
    <p>${body}</p>
    ${action || ""}
  </div>`;

const errorState = (msg, retry) => `
  <div class="error-state">
    <svg class="ic"><use href="#i-alert"/></svg>
    <div><b>Request failed</b><span>${esc(msg)}</span></div>
    ${retry ? `<button class="btn btn-ghost" onclick='(${retry})()'>Retry</button>` : ""}
  </div>`;

/* ---------- Tag input component ---------- */
function tagInput(container, initial = []) {
  const tags = new Set(initial);
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "add service… (Enter)";
  input.autocomplete = "off";

  const render = () => {
    container.innerHTML = "";
    tags.forEach((t) => {
      const chip = document.createElement("span");
      chip.className = "tag";
      chip.innerHTML = `${esc(t)}<button aria-label="remove ${esc(t)}"><svg class="ic" style="width:10px;height:10px"><use href="#i-close"/></svg></button>`;
      $("button", chip).onclick = () => { tags.delete(t); render(); };
      container.appendChild(chip);
    });
    container.appendChild(input);
  };

  const add = (v) => {
    v = v.trim().replace(/,+$/, "");
    if (!v) return;
    v.split(/[,\s]+/).filter(Boolean).forEach((t) => tags.add(t));
    input.value = "";
    render();
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(input.value); }
    else if (e.key === "Backspace" && !input.value && tags.size) {
      const last = [...tags].pop();
      tags.delete(last);
      render();
    }
  });
  input.addEventListener("blur", () => add(input.value));

  render();
  return { get: () => [...tags], set: (arr) => { tags.clear(); arr.forEach((t) => tags.add(t)); render(); } };
}

/* ============================================================
   Routing
   ============================================================ */
const ROUTES = {
  overview:     { title: "Home",         sub: "See what needs your attention", refresh: true },
  investigate:  { title: "Investigate",  sub: "Give WayPoint the evidence and find the likely cause" },
  incidents:    { title: "Incidents",    sub: "Every incident the framework has investigated", refresh: true },
  memory:       { title: "Past incidents", sub: "Find similar incidents and see how they were resolved", refresh: true },
  agents:       { title: "AI specialists", sub: "See how WayPoint compares evidence using specialist analysis", refresh: true },
  patterns:     { title: "Patterns",     sub: "Recurring failure patterns found in confirmed incidents", refresh: true },
  predictions:  { title: "Predictions",  sub: "See which failure patterns may deserve attention next", refresh: true },
  kg:           { title: "System map",   sub: "Explore services and their dependencies", refresh: true },
};

const VIEW_LOADERS = {
  overview: loadOverview,
  investigate: () => {},
  incidents: loadIncidents,
  memory: loadMemory,
  agents: loadAgents,
  patterns: loadPatterns,
  predictions: loadPredictions,
  kg: loadKg,
};

let currentRoute = "overview";

function routeFromHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const name = h.split("/")[0] || "overview";
  return ROUTES[name] ? name : "overview";
}

function setTopbar(name) {
  const meta = ROUTES[name];
  $("#view-title").textContent = meta.title;
  $("#view-sub").textContent = meta.sub;
  const actions = $("#topbar-actions");
  $("[data-topbar-refresh]", actions)?.remove();
  if (meta.refresh) {
    const btn = document.createElement("button");
    btn.className = "icon-btn";
    btn.dataset.topbarRefresh = "true";
    btn.title = "Refresh view";
    btn.innerHTML = '<svg class="ic"><use href="#i-refresh"/></svg>';
    btn.onclick = () => VIEW_LOADERS[currentRoute]?.();
    actions.appendChild(btn);
  }
}

function activateRoute(name, { initial = false } = {}) {
  currentRoute = name;
  $$(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === name));
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.route === name));
  setTopbar(name);
  VIEW_LOADERS[name]?.();
  void initial;
}

function navigate(name) {
  location.hash = `#/${name}`;
}

/* ============================================================
   Overview
   ============================================================ */
async function loadOverview() {
  const statsEl = $("#ov-stats");
  const incEl = $("#ov-incidents");
  const agentEl = $("#ov-agents");

  incEl.innerHTML = skeletonRows(6);
  agentEl.innerHTML = skeletonRows(7);
  statsEl.innerHTML = `<div class="skeleton-rows" style="grid-column:1/-1">${Array(3).fill('<div class="skeleton"></div>').join("")}</div>`;

  const [health, incidentStats, incidents, agents, pending, mstats] = await Promise.allSettled([
    api("/internal/health"), api("/incidents/stats"), api("/incidents?limit=50"), api("/agents"),
    api("/patterns/pending"), api("/memory/stats"),
  ]);

  const inc = incidents.status === "fulfilled" ? incidents.value : [];
  const ag = agents.status === "fulfilled" ? agents.value : [];
  const pend = pending.status === "fulfilled" ? pending.value : [];
  const stats = incidentStats.status === "fulfilled" ? incidentStats.value : null;
  const open = stats ? Number(stats.open || 0) : null;

  const card = (label, value, foot, cls) =>
    `<div class="stat-card ${cls || ""}"><span class="stat-label">${label}</span>
     <div class="stat-value">${value}</div>${foot ? `<div class="stat-foot">${foot}</div>` : ""}</div>`;

  statsEl.innerHTML = [
    card("Total incidents", stats ? Number(stats.total || 0) : "—", stats ? "database aggregate" : "aggregate unavailable", "acc"),
    card("Open incidents", open === null ? "—" : open, stats ? `${Number(stats.critical || 0)} critical` : "aggregate unavailable", open === null ? "slate" : (open ? "warn" : "ok")),
    card("Agents", ag.length, "registered analyzers", "acc"),
    card("Patterns pending", pend.length, pend.length ? "awaiting approval" : "none", pend.length ? "warn" : "ok"),
    card("Memory size", health.status === "fulfilled" ? health.value.memory_size : "—", "embedded incidents", "acc"),
    card("FAISS", mstats.status === "fulfilled" ? (mstats.value.faiss_enabled ? "ON" : "OFF") : "—",
      mstats.status === "fulfilled" ? (mstats.value.faiss_enabled ? "vector index live" : "fallback scan") : "unavailable",
      mstats.status === "fulfilled" && mstats.value.faiss_enabled ? "ok" : "slate"),
  ].join("");

  if (incidents.status === "rejected") {
    incEl.innerHTML = errorState(incidents.reason.message, `()=>loadOverview()`);
  } else if (!inc.length) {
    incEl.innerHTML = emptyState("No incidents yet", "File one from the Investigate tab and watch the pipeline diagnose it.",
      `<button class="btn btn-primary" onclick="navigate('investigate')">File an incident</button>`);
  } else {
    incEl.innerHTML = `
      <table class="tbl">
        <thead><tr><th>Sev</th><th>Incident</th><th>Status</th><th>Services</th><th>Created</th></tr></thead>
        <tbody>${inc.slice(0, 8).map(incidentRow).join("")}</tbody>
      </table>`;
  }

  if (agents.status === "rejected") {
    agentEl.innerHTML = errorState(agents.reason.message, `()=>loadOverview()`);
  } else if (!ag.length) {
    agentEl.innerHTML = emptyState("No agent telemetry yet", "Reliability scores appear once investigations have run.");
  } else {
    const sorted = [...ag].sort((a, b) => b.reliability - a.reliability);
    agentEl.innerHTML = `<div class="agent-bar-list">${sorted.map((a) => `
      <div class="agent-bar-row">
        <div class="agent-row-top">
          <span class="agent-row-name">${esc(a.name)}</span>
          <span class="agent-row-meta">${pct(a.reliability)}% · <b>${a.invocations}</b> inv</span>
        </div>
        ${bar(a.reliability, confCls(a.reliability))}
      </div>`).join("")}</div>`;
  }
}

function incidentRow(inc) {
  return `
    <tr data-id="${esc(inc.id)}">
      <td>${sevPill(inc.severity)}</td>
      <td><div class="tbl-title">${esc(inc.title)}</div>
          <div class="tbl-sub">${esc(inc.incident_type || "—")}</div></td>
      <td>${statusPill(inc.status)}</td>
      <td>${chips(inc.affected_services)}</td>
      <td class="tbl-sub mono" style="white-space:nowrap">${relTime(inc.created_at)}</td>
    </tr>`;
}

/* ============================================================
   Investigate
   ============================================================ */
const PIPELINE = [
  { label: "Understand", sub: "Collect the incident evidence" },
  { label: "Investigate", sub: "Compare signals and possible causes" },
  { label: "Validate", sub: "Check evidence and confidence" },
  { label: "Converge", sub: "Combine independent findings" },
  { label: "Explain", sub: "Build the root-cause explanation" },
  { label: "Recommend", sub: "Prepare the next action" },
  { label: "Learn", sub: "Remember confirmed patterns" },
];

const PRESETS = {
  memory: {
    title: "High memory pressure and OOM crashes in auth service",
    severity: "P1",
    type: "availability",
    services: ["auth-svc", "gateway", "user-db"],
    logs: [
      "<TS> auth-svc [ERROR] OutOfMemoryError: Java heap space",
      "<TS> gateway [WARN] 502 Bad Gateway upstream auth-svc failed to respond",
      "<TS> auth-svc [FATAL] Process terminated with exit code 137 (SIGKILL)",
      "<TS> k8s-node [WARN] Pod auth-svc-7d8b9-x2k4 killed by OOMKiller (memory limit 2048Mi exceeded)",
    ].join("\n"),
    metrics: JSON.stringify({ memory_utilization: 0.99, jvm_heap_used_pct: 99.8, error_rate: 0.45 }, null, 2),
    traces: JSON.stringify([
      { trace_id: "tr-oom-1", service: "gateway", duration_ms: 5400, status_code: 502 },
      { trace_id: "tr-oom-2", service: "auth-svc", duration_ms: 5000, status_code: 500 }
    ], null, 2),
  },
  db_pool: {
    title: "Payment gateway DB connection pool exhaustion",
    severity: "P1",
    type: "latency",
    services: ["payment-svc", "auth-svc", "db-svc"],
    logs: [
      "<TS> payment-svc [ERROR] Connection pool exhausted (active=100/100, waiting=245)",
      "<TS> payment-svc [ERROR] Timeout waiting for idle connection from pool after 5000ms",
      "<TS> gateway [WARN] Slow response on POST /v1/charge (avg=3200ms)",
      "<TS> db-svc [WARN] Max client connections reached on master-pg-01",
    ].join("\n"),
    metrics: JSON.stringify({ db_active_connections: 100, p99_latency_ms: 3200, pool_wait_duration_ms: 5000 }, null, 2),
    traces: JSON.stringify([
      { trace_id: "tr-db-1", service: "payment-svc", duration_ms: 3200, status_code: 504 },
      { trace_id: "tr-db-2", service: "db-svc", duration_ms: 2900, status_code: 500 }
    ], null, 2),
  },
  gateway_504: {
    title: "504 Gateway Timeout spike on /v1/checkout",
    severity: "P0",
    type: "error_rate",
    services: ["checkout-service", "payment-gateway", "inventory-svc"],
    logs: [
      "<TS> edge-gateway [ERROR] 504 Gateway Timeout downstream checkout-service",
      "<TS> checkout-service [ERROR] Downstream payment-gateway timed out after 15000ms",
      "<TS> checkout-service [WARN] Circuit breaker OPEN for payment-gateway",
      "<TS> inventory-svc [WARN] Hold lock reservation expired for order",
    ].join("\n"),
    metrics: JSON.stringify({ error_rate: 0.62, p99_latency_ms: 15200, circuit_breaker_state: 1 }, null, 2),
    traces: JSON.stringify([
      { trace_id: "tr-gw-1", service: "edge-gateway", duration_ms: 15200, status_code: 504 },
      { trace_id: "tr-gw-2", service: "checkout-service", duration_ms: 15000, status_code: 504 }
    ], null, 2),
  },
  cpu_hot: {
    title: "CPU saturation and hot-loop thread starvation in worker nodes",
    severity: "P2",
    type: "latency",
    services: ["worker-svc", "queue-svc"],
    logs: [
      "<TS> worker-svc [WARN] Thread pool thread-worker-4 high CPU utilization 100%",
      "<TS> worker-svc [ERROR] Event loop lag exceeded threshold (lag=4800ms)",
      "<TS> queue-svc [WARN] Message backlog growing (queue_depth=52400)",
    ].join("\n"),
    metrics: JSON.stringify({ cpu_utilization_pct: 99.4, event_loop_lag_ms: 4800, queue_depth: 52400 }, null, 2),
    traces: JSON.stringify([
      { trace_id: "tr-cpu-1", service: "worker-svc", duration_ms: 6800, status_code: 200 }
    ], null, 2),
  },
  cascade: {
    title: "Cascading authentication outage across microservices",
    severity: "P0",
    type: "availability",
    services: ["auth-svc", "checkout-service", "order-svc", "payment-svc"],
    logs: [
      "<TS> auth-svc [FATAL] JWT signing key rotation failed: keystore connection timeout",
      "<TS> order-svc [ERROR] 401 Unauthorized token validation rejected by auth-svc",
      "<TS> checkout-service [ERROR] Upstream auth verification failed",
      "<TS> payment-svc [ERROR] Request dropped due to unverified user context",
    ].join("\n"),
    metrics: JSON.stringify({ auth_failure_rate: 0.94, dropped_requests_pct: 88.5 }, null, 2),
    traces: JSON.stringify([
      { trace_id: "tr-cas-1", service: "checkout-service", duration_ms: 450, status_code: 401 },
      { trace_id: "tr-cas-2", service: "order-svc", duration_ms: 320, status_code: 401 }
    ], null, 2),
  },
};

let investigateState = null;
let invServices = null;

function applyPreset(key) {
  const p = PRESETS[key];
  if (!p) return;
  $("#inv-title").value = p.title;
  $("#inv-severity").value = p.severity;
  $("#inv-type").value = p.type;
  if (invServices) invServices.set(p.services);
  $("#inv-logs").value = p.logs;
  $("#inv-metrics").value = p.metrics;
  $("#inv-traces").value = p.traces;

  $$("#scenario-presets .preset-pill").forEach((b) => {
    b.classList.toggle("active", b.dataset.preset === key);
  });
}

function openDemoInvestigation() {
  navigate("investigate");
  setTimeout(() => {
    applyPreset("db_pool");
    toast("Demo incident loaded. Run the investigation when ready.", { type: "info", title: "WayPoint demo" });
    $("#inv-title")?.focus();
  }, 50);
}

function initFriendlyEvidenceToggle() {
  const toggle = $("#advanced-evidence-toggle");
  if (!toggle) return;
  const fields = $$(".advanced-evidence");
  let open = false;
  const sync = () => {
    fields.forEach((el) => el.classList.toggle("friendly-hidden", !open));
    toggle.textContent = open ? "− Hide technical evidence" : "+ Add technical evidence";
  };
  toggle.addEventListener("click", () => { open = !open; sync(); });
  sync();
}

function initInvestigate() {
  const svcEl = $("#inv-services");
  invServices = tagInput(svcEl, ["auth-svc", "gateway", "user-db"]);

  $$("#scenario-presets .preset-pill").forEach((btn) => {
    btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
  });

  applyPreset("memory");
  initFriendlyEvidenceToggle();

  $("#inv-run").addEventListener("click", runInvestigation);
  $("#investigate-demo")?.addEventListener("click", () => {
    applyPreset("db_pool");
    toast("Demo scenario loaded with logs, metrics and traces.", { type: "success", title: "Ready to run" });
  });
  $("#investigate-upload")?.addEventListener("click", () => {
    $("#inv-title")?.focus();
    $("#advanced-evidence-toggle")?.click();
    toast("Start with a short description. Technical evidence can be added below.", { type: "info", title: "Add evidence" });
  });
}

async function runInvestigation() {
  if (investigateState?.running) return;
  const title = $("#inv-title").value.trim();
  if (!title) { toast("A title is required to file an incident.", { type: "warn", title: "Missing field" }); return; }

  let metrics = {}, traces = [];
  const mRaw = $("#inv-metrics").value.trim();
  const tRaw = $("#inv-traces").value.trim();
  if (mRaw) { try { metrics = JSON.parse(mRaw); } catch (_e) { toast("Metrics must be valid JSON.", { type: "error" }); return; } }
  if (tRaw) { try { traces = JSON.parse(tRaw); } catch (_e) { toast("Traces must be a valid JSON array.", { type: "error" }); return; } }

  const payload = {
    title,
    description: null,
    severity: $("#inv-severity").value,
    incident_type: $("#inv-type").value,
    affected_services: invServices.get(),
    raw_logs: $("#inv-logs").value.split("\n").map((l) => l.trim()).filter(Boolean),
    metrics,
    traces,
    topology: {},
    context: {},
  };

  investigateState = { running: true, payload };
  $("#inv-form-wrap").classList.add("hidden");
  $("#inv-run-wrap").classList.remove("hidden");
  $("#verdict-wrap").classList.add("hidden");
  $("#verdict-wrap").innerHTML = "";
  $("#run-title").textContent = title;
  $("#inv-run").disabled = true;

  startPipeline(payload);
}

let pipelineTimer = null;

async function startPipeline(payload) {
  $("#pipeline").innerHTML = PIPELINE.map((s) => `
    <li class="pipeline-step" data-step>
      <span class="pipeline-dot"><svg class="ic"><use href="#i-check"/></svg></span>
      <span><span class="pipe-label">${esc(s.label)}</span><span class="pipe-sub">${esc(s.sub)}</span></span>
    </li>`).join("");

  const stream = $("#log-stream");
  stream.innerHTML = "";

  const t0 = Date.now();
  const tickClock = () => {
    const s = Math.floor((Date.now() - t0) / 1000);
    $("#run-clock").textContent = `0${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  pipelineTimer = setInterval(tickClock, 500);

  const setStep = (idx) => {
    const steps = $$("#pipeline .pipeline-step");
    steps.forEach((el, i) => {
      el.classList.remove("active", "done");
      if (i < idx) el.classList.add("done");
      else if (i === idx) el.classList.add("active");
    });
  };

  const log = (text, cls = "") => {
    const line = document.createElement("div");
    line.innerHTML = `<span class="ls-time">[${clock()}]</span> <span class="${cls || ""}">${esc(text)}</span>`;
    stream.appendChild(line);
    stream.scrollTop = stream.scrollHeight;
  };

  setStep(0);
  log("→ POST /incidents/investigate/stream · SSE connected", "ls-acc");

  try {
    const key = getApiKey();
    const headers = { "Content-Type": "application/json" };
    if (key) headers["X-API-Key"] = key;

    const idempotencyKey = crypto.randomUUID?.() || "inv-" + Date.now() + "-" + Math.random().toString(36).slice(2);
    headers["Idempotency-Key"] = idempotencyKey;
    const response = await fetch(BASE_API_URL + "/incidents/investigate/stream", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const errText = await response.text();
      throw new Error(errText || `HTTP ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let currentEvent = null;
    let verdictResult = null;
    let investigationResult = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) {
          currentEvent = null;
          continue;
        }
        if (trimmed.startsWith("event:")) {
          currentEvent = trimmed.slice(6).trim();
        } else if (trimmed.startsWith("data:")) {
          const rawData = trimmed.slice(5).trim();
          let data = {};
          try { data = JSON.parse(rawData); } catch (_e) { data = { message: rawData }; }

          switch (currentEvent) {
            case "heartbeat":
              log("pipeline still running · " + (data.elapsed_seconds || 0) + "s", "ls-dim");
              break;
            case "pipeline_started":
              setStep(0);
              log(`pipeline started · request_id=${data.request_id || "ok"}`);
              break;
            case "incident_persisted":
              setStep(1);
              log(`incident persisted · id=${data.incident_id?.slice(0, 10)}… · sev=${data.severity}`, "ls-ok");
              break;
            case "agents_dispatched":
              setStep(1);
              log(`dispatching agents for ${data.incident_type} incident`, "ls-acc");
              break;
            case "agent_completed":
              setStep(1);
              log(`${data.agent} ▸ [${data.finding_type}] conf=${pct(data.confidence)}%`, "ls-ok");
              if (data.summary) log(`  ↳ ${data.summary}`, "ls-dim");
              break;
            case "causal_graph_built":
              setStep(2);
              log(`causal graph constructed · nodes=${data.nodes_count} · edges=${data.edges_count}`, "ls-acc");
              break;
            case "confidence_propagated":
              setStep(3);
              log(`confidence propagated (x${data.iterations}) · candidates=${data.candidate_count}`, "ls-ok");
              if (data.top_candidate) log(`  ↳ top candidate: ${data.top_candidate}`, "ls-dim");
              break;
            case "consensus_reached":
              setStep(4);
              log(`consensus root cause: "${data.root_cause}" · conf=${pct(data.confidence)}%`, "ls-acc");
              break;
            case "explanation_built":
              setStep(5);
              log(`explanation assembled from causal graph + voters`, "ls-ok");
              break;
            case "learning_recorded":
              setStep(6);
              log(`failure pattern extracted & memory indexed`, "ls-warn");
              break;
            case "verdict":
              verdictResult = data;
              break;
            case "investigation_result":
              investigationResult = data;
              verdictResult = data;
              break;
            case "error":
              throw new Error(data.error || "Investigation stream error");
          }
        }
      }
    }

    if (verdictResult) {
      finishPipeline(verdictResult);
    } else {
      throw new Error("Pipeline completed without final verdict payload");
    }
  } catch (err) {
    failPipeline(err);
  }
}

function finishPipeline(res) {
  if (pipelineTimer) { clearInterval(pipelineTimer); pipelineTimer = null; }
  const steps = $$("#pipeline .pipeline-step");
  steps.forEach((el) => el.classList.remove("active"));
  steps.forEach((el) => el.classList.add("done"));
  const s = Math.floor(res.duration_seconds || 0);
  $("#run-clock").textContent = `0${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  logStreamLine("✓ investigation complete", "ls-ok");
  logStreamLine(`pipeline returned in ${Number(res.duration_seconds).toFixed(1)}s · ${res.agents_used?.length || 0} agents used`, "ls-acc");
  $("#verdict-wrap").classList.remove("hidden");
  $("#verdict-wrap").innerHTML = renderVerdict(res);
  setupCausalGraph($("#verdict-wrap"), (res.root_cause?.causal_chain || []));
  const conf = Number(res.root_cause?.confidence || 0);
  animateConfidence(conf);
  investigateState = { running: false, payload: null };
  $("#inv-run").disabled = false;
  state.incidentsDirty = true;
  const newBtn = document.createElement("button");
  newBtn.className = "btn btn-ghost";
  newBtn.id = "inv-new-after-success";
  newBtn.textContent = "Start another investigation";
  newBtn.onclick = () => {
    investigateState = null;
    $("#inv-run-wrap").classList.add("hidden");
    $("#inv-form-wrap").classList.remove("hidden");
    $("#inv-run").disabled = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  const command = $(".incident-command-strip", $("#verdict-wrap"));
  if (command) command.appendChild(newBtn);
}

function animateConfidence(conf) {
  const barEl = $("#verdict-bar");
  if (barEl) requestAnimationFrame(() => { barEl.style.width = `${pct(conf)}%`; });
  const countEl = $(".conf-count");
  if (!countEl) return;
  const target = Math.round(pct(conf));
  const dur = 1200;
  const t0 = performance.now();
  const step = (t) => {
    const p = Math.min((t - t0) / dur, 1);
    const e = 1 - Math.pow(1 - p, 3);
    countEl.textContent = Math.round(target * e);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function failPipeline(err) {
  if (pipelineTimer) { clearInterval(pipelineTimer); pipelineTimer = null; }
  logStreamLine("✗ investigation failed", "ls-warn");
  logStreamLine(err.message);
  $("#run-clock").textContent = "—:—";
  const payload = investigateState?.payload;
  const wrap = $("#verdict-wrap");
  wrap.classList.remove("hidden");
  wrap.innerHTML = `
    <div class="error-state" style="margin-top:20px">
      <svg class="ic"><use href="#i-alert"/></svg>
      <div><b>Investigation failed</b><span>${esc(err.message)}</span></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-primary" id="inv-retry">Try again</button>
        <button class="btn btn-ghost" id="inv-new">New investigation</button>
      </div>
    </div>`;
  $("#inv-retry").onclick = () => {
    if (payload) {
      investigateState = { running: false, payload };
      runInvestigation();
    }
  };
  $("#inv-new").onclick = () => {
    investigateState = null;
    $("#inv-run-wrap").classList.add("hidden");
    $("#inv-form-wrap").classList.remove("hidden");
    $("#inv-run").disabled = false;
  };
  investigateState = { running: false, payload };
  $("#inv-run").disabled = false;
}

function logStreamLine(text, cls) {
  const stream = $("#log-stream");
  const line = document.createElement("div");
  line.innerHTML = `<span class="ls-time">[${clock()}]</span> <span class="${cls || ""}">${text}</span>`;
  stream.appendChild(line);
  stream.scrollTop = stream.scrollHeight;
}


function renderCausalGraph(chain) {
  const nodes = (chain || []).map((n, i) => ({...n,index:i,label:n.label||n.node_id||"Unknown node",confidence:Number(n.confidence||0),kind:n.kind||"finding"}));
  if (!nodes.length) return '<div class="causal-empty">No causal graph was returned for this investigation.</div>';
  const cols=Math.min(5,Math.max(3,Math.ceil(Math.sqrt(nodes.length)))), rows=Math.ceil(nodes.length/cols), W=cols*210+70, H=rows*145+70;
  const pos=nodes.map((n,i)=>({x:55+(i%cols)*210,y:55+Math.floor(i/cols)*145}));
  const edgeHtml=nodes.slice(1).map((n,i)=>{const a=pos[i],b=pos[i+1];return '<path class="causal-edge" d="M '+a.x+' '+a.y+' L '+b.x+' '+b.y+'" marker-end="url(#causal-arrow)"></path>';}).join("");
  const nodeHtml=nodes.map((n,i)=>{const p=pos[i], confidence=Math.max(0,Math.min(1,n.confidence)), lines=String(n.label).match(/.{1,22}/g)||["Unknown"];
    const textLines=lines.slice(0,2).map((line,j)=>'<tspan x="'+p.x+'" dy="'+(j?15:0)+'">'+esc(line)+'</tspan>').join("");
    return '<g class="causal-node" tabindex="0" role="button" aria-label="Inspect '+esc(n.label)+'" data-causal-index="'+i+'" transform="translate('+p.x+' '+p.y+')">'+
      '<circle r="25" class="causal-node-ring"></circle><circle r="18" class="causal-node-core" style="stroke-dasharray:'+(113*confidence).toFixed(1)+' 113"></circle>'+
      '<text class="causal-node-num" y="4">'+(i+1)+'</text><text class="causal-node-label" y="48">'+textLines+'</text>'+
      '<text class="causal-node-kind" y="82">'+esc(n.kind)+' · '+pct(confidence)+'%</text></g>';
  }).join("");
  return '<div class="causal-graph-shell"><div class="causal-graph-toolbar"><div><b>Evidence convergence map</b><span>Click a node to inspect its contribution to the root cause.</span></div>'+
    '<div class="causal-graph-actions"><button class="icon-btn causal-zoom" data-causal-zoom="-1" aria-label="Zoom out">−</button><button class="icon-btn causal-zoom" data-causal-zoom="1" aria-label="Zoom in">+</button><button class="icon-btn causal-zoom" data-causal-reset="1" aria-label="Reset zoom">⟳</button></div></div>'+
    '<div class="causal-graph-viewport"><svg class="causal-svg" viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Interactive causal path"><defs><marker id="causal-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="currentColor"></path></marker></defs><g class="causal-zoom-layer">'+edgeHtml+nodeHtml+'</g></svg>'+
    '<div class="causal-inspector" data-causal-inspector><div class="causal-inspector-empty">Select a node to inspect it.</div></div></div></div>';
}
function setupCausalGraph(root, chain) {
  const svg=root.querySelector(".causal-svg"), layer=root.querySelector(".causal-zoom-layer"), inspector=root.querySelector("[data-causal-inspector]");
  if(!svg||!layer||!inspector)return;
  let zoom=1; const renderZoom=()=>layer.setAttribute("transform","scale("+zoom+")");
  const inspect=(index)=>{const n=chain[index];if(!n)return;root.querySelectorAll(".causal-node").forEach(el=>el.classList.toggle("selected",Number(el.dataset.causalIndex)===index));
    inspector.innerHTML='<div class="causal-inspector-kicker">NODE '+String(index+1).padStart(2,"0")+'</div><div class="causal-inspector-title">'+esc(n.label||n.node_id||"Unknown")+'</div>'+
      '<div class="causal-inspector-grid"><span><i>type</i><b>'+esc(n.kind||"finding")+'</b></span><span><i>confidence</i><b>'+pct(n.confidence)+'%</b></span><span><i>source</i><b>'+esc(n.source_agent||"orchestrator")+'</b></span><span><i>node id</i><b>'+esc(n.node_id||"—")+'</b></span></div>';};
  root.querySelectorAll(".causal-node").forEach(node=>{node.addEventListener("click",()=>inspect(Number(node.dataset.causalIndex)));node.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();inspect(Number(node.dataset.causalIndex));}});});
  root.querySelectorAll("[data-causal-zoom]").forEach(btn=>btn.addEventListener("click",()=>{zoom=Math.max(.65,Math.min(1.8,zoom+Number(btn.dataset.causalZoom)*.15));renderZoom();}));
  root.querySelector("[data-causal-reset]")?.addEventListener("click",()=>{zoom=1;renderZoom();}); renderZoom(); if(chain.length)inspect(chain.length-1);
}

function renderVerdict(res) {
  const rc = res.root_cause || {};
  const exp = res.explanation || {};
  const meta = res.meta_reasoning || {};
  const conf = Number(rc.confidence || 0);
  const alts = [...(rc.alternatives || [])].sort((a, b) => b.confidence - a.confidence);
  const chain = rc.causal_chain || [];
  const factors = rc.contributing_factors || [];
  const agents = res.agents_used || [];
  const evidence = exp.evidence_used || [];
  const agentStatuses = res.agent_statuses || [];
  const runtime = res.runtime || {};

  const confMeter = `
    <div class="conf-meter">
      <div class="conf-top"><span>CONFIDENCE</span>
        <span class="conf-num"><span class="conf-count">0</span><small>%</small></span></div>
      <div class="bar-track" style="height:9px"><div class="bar-fill ${confCls(conf)}" id="verdict-bar" style="width:0%"></div></div>
    </div>`;

  const chainHtml = renderCausalGraph(chain);

  const altsHtml = alts.length
    ? alts.map((a) => `
      <div class="alt-item">
        <div class="alt-cause"><span>${esc(a.cause)}</span><span class="mono" style="color:var(--accent-strong)">${pct(a.confidence)}%</span></div>
        ${bar(a.confidence, confCls(a.confidence))}
        ${a.evidence && a.evidence.length ? `<div class="alt-evidence">${a.evidence.map((ev) => `<span class="chip">${esc(ev)}</span>`).join("")}</div>` : ""}
      </div>`).join("")
    : `<p class="hint">No alternative hypotheses ranked.</p>`;

  const evidenceHtml = evidence.length
    ? '<div class="evidence-grid">' + evidence.slice(0, 12).map((item, i) => {
        const source = item.source || item.agent || item.type || item.kind || "Evidence";
        const detail = item.detail || item.description || item.summary || item.value || item.evidence || "";
        const score = item.confidence ?? item.weight ?? item.relevance;
        return '<article class="evidence-card">' +
          '<div class="evidence-top"><span class="evidence-index">' + String(i + 1).padStart(2, "0") + '</span>' +
          '<span class="evidence-source">' + esc(source) + '</span>' +
          (score != null ? '<span class="evidence-score">' + pct(Number(score)) + '%</span>' : '') + '</div>' +
          '<div class="evidence-detail">' + esc(typeof detail === "object" ? JSON.stringify(detail) : detail) + '</div></article>';
      }).join("") + '</div>'
    : '<div class="empty-inline">No structured evidence was returned by the explainability engine.</div>';

  const agentStatusHtml = agentStatuses.length
    ? '<div class="agent-investigation-grid">' + agentStatuses.map((a) => {
        const status = a.status || "unknown";
        return '<div class="agent-investigation-card agent-' + esc(status) + '">' +
          '<div class="agent-investigation-head"><span class="agent-state-dot"></span>' +
          '<b>' + esc(a.agent_name || "unknown") + '</b><span class="agent-status-label">' + esc(status) + '</span></div>' +
          '<div class="agent-investigation-meta"><span>' + esc(a.finding_type || "analysis") + '</span>' +
          '<span>' + Number(a.execution_ms || 0).toFixed(0) + ' ms</span></div></div>';
      }).join("") + '</div>'
    : '<div class="empty-inline">Agent execution telemetry was not returned.</div>';

  const suggestions = meta.suggestions || [];
  const suggestionsHtml = suggestions.length
    ? '<div class="action-list">' + suggestions.map((s, i) =>
        '<div class="action-item"><span>' + (i + 1) + '</span><p>' + esc(s) + '</p></div>'
      ).join("") + '</div>'
    : '<div class="empty-inline">No operational suggestions were returned for this investigation.</div>';

  const finalExplanation = exp.final_explanation || rc.explanation || "No explanation available.";

  return `
    <div class="verdict">
      <div class="verdict-head">
        <div>
          <span class="verdict-status">ROOT CAUSE IDENTIFIED</span>
          <div class="verdict-title">${esc(res.root_cause?.root_cause || "Root cause determined")}</div>
        </div>
        <div style="text-align:right; display:flex; flex-direction:column; gap:8px; align-items:flex-end">
          ${monoId(res.incident_id)}
          <button class="btn btn-ghost" data-open-incident="${esc(res.incident_id)}">Open incident <svg class="ic" style="width:13px;height:13px"><use href="#i-arrow"/></svg></button>
        </div>
      </div>

      <div class="incident-command-strip">
        <div><span>SEVERITY</span><strong>${esc(res.severity || "—")}</strong></div>
        <div><span>TYPE</span><strong>${esc(res.incident_type || "—")}</strong></div>
        <div><span>AGENTS</span><strong>${agents.length || agentStatuses.length}</strong></div>
        <div><span>DURATION</span><strong>${Number(res.duration_seconds || 0).toFixed(1)}s</strong></div>
        <div class="incident-services"><span>AFFECTED SERVICES</span><div>${(res.affected_services || []).map((s) => '<span class="chip">' + esc(s) + '</span>').join("") || '<span class="hint">—</span>'}</div></div>
      </div>
      <div class="verdict-overview-grid">
        <div class="root-cause-box">
          <div class="root-cause-text">
            <div class="rc-label">Most likely cause</div>
            <div class="rc-value">${esc(rc.root_cause || "—")}</div>
          </div>
          <div class="root-cause-meter">${confMeter}</div>
        </div>
        <div class="verdict-signal-card">
          <span class="rc-label">Investigation signal</span>
          <strong>${agents.length || agentStatuses.length}</strong>
          <span>specialists compared the evidence</span>
          <div class="signal-foot">${agentStatuses.filter((a) => a.status === "ok").length} healthy · ${agentStatuses.filter((a) => a.status === "failed").length} degraded</div>
        </div>
      </div>

      <div class="drawer-section verdict-section">
        <div class="section-heading-row"><span class="sec-label">Why WayPoint believes this</span><span class="hint">How the evidence converged</span></div>
        ${chainHtml}
      </div>

      <div class="friendly-action-card"><div><span class="rc-label">RECOMMENDED ACTION</span><p>Review the evidence below, then confirm the suggested remediation.</p></div></div>
      <div class="drawer-section verdict-section">
        <div class="section-heading-row"><span class="sec-label">Supporting evidence</span><span class="hint">${evidence.length} structured item${evidence.length === 1 ? "" : "s"}</span></div>
        ${evidenceHtml}
      </div>

      <div class="drawer-section">
        <span class="sec-label">Other possible causes</span>
        ${altsHtml}
      </div>

      ${factors.length ? `<div class="drawer-section">
        <span class="sec-label">Contributing factors</span>
        <div class="factor-list">${factors.map((f) => `<span class="chip">${esc(f)}</span>`).join("")}</div>
      </div>` : ""}

      <div class="drawer-section">
        <span class="sec-label">What happened</span>
        <div class="explanation-text">${esc(finalExplanation)}</div>
      </div>

      <div class="drawer-section verdict-section">
        <div class="section-heading-row"><span class="sec-label">Technical analysis</span><span class="hint">Optional specialist details</span></div>
        ${agentStatusHtml}
      </div>

      <div class="drawer-section verdict-section">
        <div class="section-heading-row"><span class="sec-label">Recommended next steps</span><span class="hint">Review before taking action</span></div>
        ${suggestionsHtml}
      </div>

      <div class="drawer-section">
        <details class="meta-box">
          <summary>Meta reasoning</summary>
          <div class="meta-inner">
            ${meta.optimal_path ? `<div class="meta-row"><b>Optimal path</b>${esc(meta.optimal_path)}</div>` : ""}
            ${exp.graph_reasoning ? `<div class="meta-row"><b>Graph reasoning</b>${esc(exp.graph_reasoning)}</div>` : ""}
            ${meta.suggestions?.length ? `<div class="meta-row"><b>Suggestions</b><span>${meta.suggestions.map((s) => `· ${esc(s)}`).join("<br>")}</span></div>` : ""}
            ${meta.agent_scores && Object.keys(meta.agent_scores).length ? `<div class="meta-row"><b>Agent scores</b><div class="agent-chips" style="margin-top:6px">${Object.entries(meta.agent_scores).map(([k, v]) => `<span class="agent-chip">${esc(k)} <b style="color:var(--accent-strong)">${Number(v).toFixed(2)}</b></span>`).join("")}</div></div>` : ""}
            ${meta.unnecessary_agents?.length ? `<div class="meta-row"><b>Unnecessary agents</b>${meta.unnecessary_agents.map((a) => `<span class="agent-chip" style="margin-left:6px">${esc(a)}</span>`).join("")}</div>` : ""}
            ${Object.keys(runtime).length ? `<div class="meta-row"><b>Runtime</b><span class="runtime-grid">${Object.entries(runtime).slice(0, 8).map(([k,v]) => `<span><i>${esc(k)}</i>${esc(v)}</span>`).join("")}</span></div>` : ""}
          </div>
        </details>
      </div>
    </div>`;
}

/* ============================================================
   Incidents
   ============================================================ */
const state = {
  incidents: [],
  incidentsDirty: true,
  filters: { q: "", sev: "all", status: "all" },
  memoryLoaded: false,
  agentsLoaded: false,
  patternsLoaded: false,
  predictionsLoaded: false,
  kgLoaded: false,
};

function initIncidents() {
  const sevEl = $("#inc-sev-filter");
  ["all", "P0", "P1", "P2", "P3", "P4"].forEach((s) => {
    const b = document.createElement("button");
    b.textContent = s === "all" ? "All" : s;
    b.dataset.sev = s;
    if (s === "all") b.classList.add("active");
    b.onclick = () => {
      state.filters.sev = s;
      $$("#inc-sev-filter button").forEach((x) => x.classList.toggle("active", x === b));
      renderIncidentList();
    };
    sevEl.appendChild(b);
  });

  const stEl = $("#inc-status-filter");
  ["all", "active", "investigating", "resolved"].forEach((s) => {
    const b = document.createElement("button");
    b.textContent = s === "all" ? "All" : s;
    b.dataset.status = s;
    if (s === "all") b.classList.add("active");
    b.onclick = () => {
      state.filters.status = s;
      $$("#inc-status-filter button").forEach((x) => x.classList.toggle("active", x === b));
      renderIncidentList();
    };
    stEl.appendChild(b);
  });

  $("#inc-search").addEventListener("input", (e) => {
    state.filters.q = e.target.value.toLowerCase().trim();
    renderIncidentList();
  });
  $("#inc-refresh").addEventListener("click", loadIncidents);
}

async function loadIncidents() {
  const listEl = $("#inc-list");
  listEl.innerHTML = skeletonRows(7);
  try {
    state.incidents = await api("/incidents?limit=50");
    state.incidentsDirty = false;
    renderIncidentList();
  } catch (err) {
    listEl.innerHTML = errorState(err.message, `()=>loadIncidents()`);
  }
}

function renderIncidentList() {
  const listEl = $("#inc-list");
  const { q, sev, status } = state.filters;
  const rows = state.incidents.filter((i) => {
    if (sev !== "all" && i.severity !== sev) return false;
    if (status !== "all" && (i.status || "").toLowerCase() !== status) return false;
    if (q) {
      const hay = `${i.title} ${i.id} ${i.incident_type || ""} ${(i.affected_services || []).join(" ")}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  $("#inc-count").textContent = `${rows.length} of ${state.incidents.length} incidents`;

  if (!rows.length) {
    listEl.innerHTML = state.incidents.length
      ? emptyState("No matching incidents", "Try clearing the search or adjusting the severity / status filters.")
      : emptyState("No incidents yet", "File one from the Investigate tab and watch the pipeline diagnose it.",
          `<button class="btn btn-primary" onclick="navigate('investigate')">File an incident</button>`);
    return;
  }

  listEl.innerHTML = `
    <table class="tbl">
      <thead><tr><th>Sev</th><th>Incident</th><th>Status</th><th>Services</th><th>Created</th></tr></thead>
      <tbody>${rows.map(incidentRow).join("")}</tbody>
    </table>`;
}

/* ============================================================
   Memory
   ============================================================ */
function initMemory() {
  $("#mem-search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") searchMemory($("#mem-search").value.trim());
  });
  $("#mem-search").addEventListener("input", (e) => {
    if (!e.target.value.trim()) renderMemoryEmpty();
  });
}

async function loadMemory() {
  if (state.memoryLoaded) return;
  try {
    const stats = await api("/memory/stats");
    $("#mem-size").textContent = stats.size;
    $("#mem-faiss").textContent = stats.faiss_enabled ? "ON" : "OFF";
    $("#mem-faiss").style.color = stats.faiss_enabled ? "var(--ok)" : "var(--text-faint)";
  } catch (_e) { /* stats are decorative */ }
  state.memoryLoaded = true;
  renderMemoryEmpty();
}

function renderMemoryEmpty() {
  const el = $("#memory-results");
  el.innerHTML = emptyState("Search incident memory",
    "Type a query above — e.g. <b>payment timeout</b> — and press Enter to retrieve semantically similar past incidents with their root causes.");
}

async function searchMemory(q) {
  const el = $("#memory-results");
  if (!q) { renderMemoryEmpty(); return; }
  el.innerHTML = `<div class="skeleton-rows">${Array(4).fill('<div class="skeleton"></div>').join("")}</div>`;
  try {
    const hits = await api("/memory/search", { method: "POST", body: { query: q, top_k: 5, similarity_threshold: 0 } });
    if (!hits.length) {
      el.innerHTML = emptyState("No similar incidents found",
        "Nothing in memory is semantically close to that query. Run an investigation to grow the memory store.");
      return;
    }
    el.innerHTML = `<div class="mem-stack">${hits.map((h) => `
      <div class="mem-card" data-open-incident="${esc(h.incident_id)}" role="button" tabindex="0">
        <div class="mem-top">
          ${monoId(h.incident_id)}
          <span class="hint mono">conf ${pct(h.confidence)}%</span>
        </div>
        <div class="mem-cause">${esc(h.root_cause || "—")}</div>
        <div class="mem-sim-row">
          <span class="mem-sim-label">SIM</span>
          ${bar(h.similarity, "ok")}
          <span class="mem-sim-pct">${pct(h.similarity)}%</span>
        </div>
        ${h.resolution ? `<div class="mem-res"><b>Resolution</b> · ${esc(h.resolution)}</div>` : ""}
        ${h.services?.length ? `<div class="mem-res"><b>Services</b></div><div class="chips" style="margin-top:4px">${h.services.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</div>` : ""}
      </div>`).join("")}</div>`;
  } catch (err) {
    el.innerHTML = errorState(err.message, `()=>searchMemory(${JSON.stringify(q)})`);
  }
}

/* ============================================================
   Agents
   ============================================================ */
async function loadAgents() {
  if (state.agentsLoaded) return;
  const grid = $("#agent-grid");
  grid.innerHTML = skeletonRows(8);
  try {
    const agents = await api("/agents");
    state.agentsLoaded = true;
    if (!agents.length) {
      grid.innerHTML = emptyState("No agent telemetry yet", "Reliability scores appear once investigations have run.");
      return;
    }
    const sorted = [...agents].sort((a, b) => b.reliability - a.reliability);
    grid.innerHTML = sorted.map((a) => `
      <div class="agent-card">
        <div>
          <div class="agent-name">${esc(a.name)}</div>
          <div class="agent-sub">analysis agent</div>
        </div>
        <div class="agent-num">${pct(a.reliability)}<small>% reliable</small></div>
        ${bar(a.reliability, confCls(a.reliability))}
        <div class="hint mono">${a.invocations} invocations</div>
      </div>`).join("");
  } catch (err) {
    grid.innerHTML = errorState(err.message, `()=>loadAgents()`);
  }
}

/* ============================================================
   Patterns
   ============================================================ */

async function matchPatterns() {
  const logs = $("#pat-logs").value.split("\n").map((l) => l.trim()).filter(Boolean);
  const out = $("#pat-match-results");
  if (!logs.length) { toast("Paste at least one log line to match.", { type: "warn" }); return; }
  out.innerHTML = `<div class="skeleton-rows">${Array(2).fill('<div class="skeleton"></div>').join("")}</div>`;
  try {
    const hits = await api("/patterns/match", { method: "POST", body: { logs } });
    out.innerHTML = hits.length
      ? `<div class="match-res">${hits.map((h) => `
          <div class="match-hit">
            ${monoId(h.pattern_id)}
            <div style="flex:1;min-width:0">
              <div class="hint" style="color:var(--text);font-family:var(--mono);font-size:11.5px;word-break:break-all">${esc(h.matched_signature)}</div>
              <div class="hint" style="margin-top:3px">${esc(h.root_cause_hint)}</div>
            </div>
            <span class="pill ${confCls(h.confidence) === "ok" ? "pill-ok" : confCls(h.confidence) === "warn" ? "pill-warn" : "pill-bad"}">${pct(h.confidence)}%</span>
          </div>`).join("")}</div>`
      : emptyState("No pattern matches", "None of the approved patterns matched these logs.");
  } catch (err) {
    out.innerHTML = errorState(err.message, `()=>matchPatterns()`);
  }
}

function initPatterns() {
  $("#pat-match").addEventListener("click", async () => {
    const logs = $("#pat-logs").value.split("\n").map((l) => l.trim()).filter(Boolean);
    const out = $("#pat-match-results");
    if (!logs.length) { toast("Paste at least one log line to match.", { type: "warn" }); return; }
    out.innerHTML = `<div class="skeleton-rows">${Array(2).fill('<div class="skeleton"></div>').join("")}</div>`;
    try {
      const hits = await api("/patterns/match", { method: "POST", body: { logs } });
      out.innerHTML = hits.length
        ? `<div class="match-res">${hits.map((h) => `
            <div class="match-hit">
              ${monoId(h.pattern_id)}
              <div style="flex:1;min-width:0">
                <div class="hint" style="color:var(--text);font-family:var(--mono);font-size:11.5px;word-break:break-all">${esc(h.matched_signature)}</div>
                <div class="hint" style="margin-top:3px">${esc(h.root_cause_hint)}</div>
              </div>
              <span class="pill ${confCls(h.confidence) === "ok" ? "pill-ok" : confCls(h.confidence) === "warn" ? "pill-warn" : "pill-bad"}">${pct(h.confidence)}%</span>
            </div>`).join("")}</div>`
        : emptyState("No pattern matches", "None of the approved patterns matched these logs.");
    } catch (err) {
      out.innerHTML = errorState(err.message, `()=>matchPatterns()`);
    }
  });
}

async function loadPatterns() {
  const pendingEl = $("#pat-pending");
  const approvedEl = $("#pat-approved");
  pendingEl.innerHTML = skeletonRows(4);
  approvedEl.innerHTML = skeletonRows(2);
  try {
    const [pending, approved] = await Promise.all([
      api("/patterns/pending"), api("/patterns/approved"),
    ]);
    state.patternsLoaded = true;
    $("#pat-pending-count").textContent = pending.length;

    pendingEl.innerHTML = pending.length
      ? `<div class="list-stack">${pending.map(renderPatCard).join("")}</div>`
      : emptyState("Nothing pending", "New failure patterns appear here after each investigation, awaiting approval.");

    approvedEl.innerHTML = approved.length
      ? `<div class="list-stack">${approved.map(renderPatCard).join("")}</div>`
      : emptyState("No approved patterns", "Approved signatures will appear here and are used by the match tool.");

    $$("#pat-pending [data-approve]").forEach((b) => {
      b.onclick = async () => {
        const id = b.dataset.id;
        b.disabled = true;
        try {
          await api(`/patterns/${id}/approve`, { method: "POST", body: { pattern_id: id, approver: "console" } });
          toast("Pattern approved and promoted to production matching.", { type: "success", title: "Pattern approved" });
          loadPatterns();
        } catch (err) {
          toast(err.message, { type: "error", title: "Approval failed" });
          b.disabled = false;
        }
      };
    });
  } catch (err) {
    pendingEl.innerHTML = errorState(err.message, `()=>loadPatterns()`);
    approvedEl.innerHTML = "";
  }
}

function renderPatCard(p) {
  return `
    <div class="pat-card">
      <div class="pat-text">${esc(p.pattern_text)}</div>
      <div class="pat-hint"><b>hint</b> · ${esc(p.root_cause_hint)}</div>
      <div class="pat-meta">
        ${p.approved ? `<span class="pill pill-ok">approved</span>` : `<span class="pill pill-warn">pending</span>`}
        <span class="hint mono">occ ×${p.occurrence_count}</span>
        ${monoId(p.id)}
      </div>
      <div class="pat-foot">
        <div class="pat-conf"><span>conf</span>${bar(p.confidence, confCls(p.confidence))}<span>${pct(p.confidence)}%</span></div>
        ${p.approved ? "" : `<button class="btn btn-primary" data-id="${esc(p.id)}" data-approve>Approve</button>`}
      </div>
    </div>`;
}

/* ============================================================
   Predictions
   ============================================================ */
function initPredictions() {
  $("#pred-run").addEventListener("click", async () => {
    const btn = $("#pred-run");
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '<span class="hint" style="color:var(--accent-ink)">running…</span>';
    try {
      const preds = await api("/predictions/run", { method: "POST", body: {} });
      renderPredictions(preds);
      toast(
        preds.length
          ? `${preds.length} prediction(s) generated.`
          : "Analysis completed — no significant failure risks were detected.",
        { type: "success" }
      );
    } catch (err) {
      toast(err.message, { type: "error", title: "Prediction run failed" });
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  });
}

async function loadPredictions() {
  const list = $("#pred-list");
  if (!state.predictionsLoaded) list.innerHTML = skeletonRows(4);
  try {
    const preds = await api("/predictions");
    state.predictionsLoaded = true;
    renderPredictions(preds);
  } catch (err) {
    if (!state.predictionsLoaded) list.innerHTML = errorState(err.message, `()=>loadPredictions()`);
  }
}

function renderPredictions(preds) {
  const list = $("#pred-list");
  if (!preds.length) {
    list.innerHTML = emptyState("No predictions yet",
      "Run the prediction engine to forecast the next likely failure from historical recurrence and trend analysis.",
      `<button class="btn btn-primary" id="pred-run-empty">Run predictions</button>`);
    const b = $("#pred-run-empty");
    if (b) b.onclick = () => $("#pred-run").click();
    return;
  }
  const sorted = [...preds].sort((a, b) => b.probability - a.probability);
  list.innerHTML = sorted.map((p) => {
    const prob = Number(p.probability || 0);
    const barCls = prob >= 0.6 ? "bad" : prob >= 0.3 ? "warn" : "ok";
    const impactCls = (p.impact || "low").toLowerCase();
    return `
    <div class="pred-card">
      <div class="pred-top">
        <span class="pred-service">${esc(p.service)}</span>
        <span class="pred-type mono">${esc(p.predicted_failure_type || "—")}</span>
        <span class="impact-badge impact-${impactCls}">${esc(p.impact || "—")}</span>
        ${p.estimated_time_minutes != null ? `<span class="hint mono">est +${p.estimated_time_minutes}min</span>` : ""}
        ${p.updated_at ? `<span class="hint mono">${relTime(p.updated_at)}</span>` : ""}
      </div>
      <div class="pred-prob-row">
        <span class="pred-prob-label">PROBABILITY</span>
        ${bar(prob, barCls)}
        <span class="pred-prob-pct" style="color:${barCls === "ok" ? "var(--ok)" : barCls === "warn" ? "var(--warn)" : "var(--bad)"}">${pct(prob)}%</span>
      </div>
      ${p.rationale ? `<div class="pred-rationale">${esc(p.rationale)}</div>` : ""}
    </div>`;
  }).join("");
}

/* ============================================================
   Knowledge Graph
   ============================================================ */
let kgServices = null;
let kgState = {
  data: null,
  pos: {},
  zoom: 1,
  panX: 0,
  panY: 0,
  draggingNode: null,
  panning: false,
  startPointer: { x: 0, y: 0 },
};

function initKg() {
  kgServices = tagInput($("#kg-services"), ["checkout-service", "payment-gateway", "auth-service"]);

  $("#kg-build").addEventListener("click", buildKg);
  $("#kg-services input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") buildKg();
  });

  $("#kg-zoom-in")?.addEventListener("click", () => {
    kgState.zoom = Math.min(kgState.zoom * 1.25, 3.5);
    updateKgTransform();
  });

  $("#kg-zoom-out")?.addEventListener("click", () => {
    kgState.zoom = Math.max(kgState.zoom / 1.25, 0.4);
    updateKgTransform();
  });

  $("#kg-zoom-reset")?.addEventListener("click", () => {
    kgState.zoom = 1;
    kgState.panX = 0;
    kgState.panY = 0;
    updateKgTransform();
  });

  const svg = $("#kg-svg");
  if (svg) {
    svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const delta = e.deltaY < 0 ? 1.12 : 0.89;
      kgState.zoom = Math.max(0.35, Math.min(3.5, kgState.zoom * delta));
      updateKgTransform();
    }, { passive: false });

    svg.addEventListener("pointerdown", (e) => {
      const nodeEl = e.target.closest("[data-node-id]");
      if (nodeEl) {
        kgState.draggingNode = nodeEl.dataset.nodeId;
        svg.classList.add("grabbing");
        nodeEl.setPointerCapture?.(e.pointerId);
      } else {
        kgState.panning = true;
        kgState.startPointer = { x: e.clientX - kgState.panX, y: e.clientY - kgState.panY };
        svg.classList.add("grabbing");
      }
    });

    svg.addEventListener("pointermove", (e) => {
      if (kgState.draggingNode && kgState.pos[kgState.draggingNode]) {
        const rect = svg.getBoundingClientRect();
        const svgX = ((e.clientX - rect.left) / rect.width) * 800;
        const svgY = ((e.clientY - rect.top) / rect.height) * 420;
        const x = (svgX - kgState.panX) / kgState.zoom;
        const y = (svgY - kgState.panY) / kgState.zoom;
        kgState.pos[kgState.draggingNode].x = x;
        kgState.pos[kgState.draggingNode].y = y;
        drawKgElements();
      } else if (kgState.panning) {
        kgState.panX = e.clientX - kgState.startPointer.x;
        kgState.panY = e.clientY - kgState.startPointer.y;
        updateKgTransform();
      }
    });

    const endDrag = () => {
      kgState.draggingNode = null;
      kgState.panning = false;
      svg.classList.remove("grabbing");
    };
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", endDrag);

    svg.addEventListener("click", (e) => {
      const nodeEl = e.target.closest("[data-node-id]");
      if (nodeEl) {
        const id = nodeEl.dataset.nodeId;
        const nd = (kgState.data?.nodes || []).find((n) => n.id === id);
        if (nd) inspectKgNode(nd);
      } else if (!e.target.closest(".kg-node-inspector")) {
        $("#kg-node-inspector")?.classList.add("hidden");
      }
    });
  }

  $("#kg-svc-add").addEventListener("click", async () => {
    const name = $("#kg-svc-name").value.trim();
    if (!name) { toast("Service name is required.", { type: "warn" }); return; }
    try {
      await api("/kg/services", { method: "POST", body: { name, team: $("#kg-svc-team").value.trim() || null, tier: $("#kg-svc-tier").value || null } });
      // Add new service to current selection if not already present
      const current = kgServices.get();
      if (!current.includes(name)) {
        kgServices.set([...current, name]);
      }
      // Refresh the graph if we have services selected
      if (kgServices?.get().length) {
        await buildKg();
      }
      toast(`Service "${name}" upserted into the knowledge graph.`, { type: "success" });
      ["#kg-svc-name", "#kg-svc-team"].forEach((s) => $(s).value = "");
      $("#kg-svc-tier").value = "";
    } catch (err) { toast(err.message, { type: "error", title: "Upsert failed" }); }
  });

  $("#kg-api-add").addEventListener("click", async () => {
    const path = $("#kg-api-path").value.trim();
    if (!path) { toast("API path is required.", { type: "warn" }); return; }
    try {
      await api("/kg/apis", { method: "POST", body: { path, method: $("#kg-api-method").value.trim() || "GET", service: $("#kg-api-service").value.trim() || null } });
      // Refresh the graph if we have services selected
      if (kgServices?.get().length) {
        await buildKg();
      }
      toast(`API ${path} upserted.`, { type: "success" });
      $("#kg-api-path").value = ""; $("#kg-api-service").value = "";
    } catch (err) { toast(err.message, { type: "error", title: "Upsert failed" }); }
  });

  $("#kg-dep-add").addEventListener("click", async () => {
    const name = $("#kg-dep-name").value.trim();
    const dep = $("#kg-dep-target").value.trim();
    if (!name || !dep) { toast("Both service names are required for a dependency.", { type: "warn" }); return; }
    try {
      await api(`/kg/services/${encodeURIComponent(name)}/depends-on/${encodeURIComponent(dep)}`, { method: "POST" });
      // Refresh the graph if we have services selected
      if (kgServices?.get().length) {
        await buildKg();
      }
      toast(`Dependency ${name} → ${dep} recorded.`, { type: "success" });
      $("#kg-dep-name").value = ""; $("#kg-dep-target").value = "";
    } catch (err) { toast(err.message, { type: "error", title: "Link failed" }); }
  });
}

function updateKgTransform() {
  const g = $("#kg-svg-root");
  if (g) {
    g.setAttribute("transform", `translate(${kgState.panX}, ${kgState.panY}) scale(${kgState.zoom})`);
  }
}

function inspectKgNode(nd) {
  const insp = $("#kg-node-inspector");
  if (!insp) return;
  insp.classList.remove("hidden");
  const props = nd.props || {};
  const c = kgColor(nd.label);

  const edges = kgState.data?.edges || [];
  const inEdges = edges.filter((e) => e.target === nd.id).map((e) => e.source);
  const outEdges = edges.filter((e) => e.source === nd.id).map((e) => e.target);

  insp.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div class="kg-insp-title" style="color:${c}">${esc(nd.key)}</div>
      <button class="icon-btn" style="width:20px;height:20px" onclick="$('#kg-node-inspector').classList.add('hidden')">&times;</button>
    </div>
    <div class="kg-insp-type">${esc(nd.label || "NODE")}</div>
    ${Object.entries(props).map(([k, v]) => `
      <div class="kg-insp-prop"><b>${esc(k)}:</b> ${esc(v)}</div>
    `).join("")}
    ${inEdges.length ? `<div class="kg-insp-prop" style="margin-top:6px"><b>Inbound:</b> ${inEdges.map(esc).join(", ")}</div>` : ""}
    ${outEdges.length ? `<div class="kg-insp-prop"><b>Outbound:</b> ${outEdges.map(esc).join(", ")}</div>` : ""}
  `;
}

function renderFallbackKg(incidents, selectedServices) {
  const services = [...new Set(selectedServices.map(s => String(s).trim()).filter(Boolean))];
  const nodes = [];
  const edges = [];
  const seen = new Set();

  const addNode = (id, label, key, props = {}) => {
    if (seen.has(id)) return;
    seen.add(id);
    nodes.push({ id, label, key, props });
  };

  services.forEach(s => addNode("svc:" + s, "service", s, { source: "incident history" }));
  (incidents || []).forEach((inc) => {
    const affected = (inc.affected_services || []).filter(s => services.includes(s));
    if (!affected.length) return;
    const iid = "inc:" + inc.id;
    addNode(iid, "incident", trunc(inc.title || inc.id, 28), {
      severity: inc.severity || "—",
      status: inc.status || "—",
    });
    affected.forEach(s => edges.push({ source: "svc:" + s, target: iid, rel: "AFFECTED_BY", weight: 1 }));
    for (let i = 0; i < affected.length; i++) {
      for (let j = i + 1; j < affected.length; j++) {
        edges.push({ source: "svc:" + affected[i], target: "svc:" + affected[j], rel: "CO_OCCURS", weight: 0.55 });
      }
    }
  });

  renderKG({
    nodes,
    edges,
    meta: { source: "postgres_incident_history", fallback: true },
  });

  const meta = $("#pred-meta");
  void meta;
  const stage = $("#kg-stage");
  if (stage) {
    const note = document.createElement("div");
    note.className = "kg-fallback-note";
    note.textContent = "Incident topology · derived from stored WayPoint incident history";
    stage.prepend(note);
  }
}

async function loadKg() {
  if (state.kgLoaded) return;
  state.kgLoaded = true;
  const services = kgServices?.get() || [];

  try {
    const health = await api("/internal/health");
    if (health?.subsystems?.neo4j === "connected") {
      if (services.length) await buildKg();
      return;
    }
  } catch (_e) {
    // Fall through to the database-backed demo graph.
  }

  if (services.length) await buildKg();
}

async function buildKg() {
  const services = kgServices?.get() || [];
  if (!services.length) {
    toast("Add at least one service to build a subgraph.", { type: "warn" });
    return;
  }

  const canvas = $("#kg-canvas");
  const empty = $("#kg-empty");
  canvas.classList.remove("hidden");
  empty.classList.add("hidden");
  $("#kg-svg").innerHTML = "";

  try {
    const health = await api("/internal/health");
    if (health?.subsystems?.neo4j === "connected") {
      const data = await api("/kg/services/subgraph", { method: "POST", body: { services } });
      renderKG(data);
      return;
    }
  } catch (_e) {
    // Use the real incident-history fallback below.
  }

  try {
    const incidents = await api("/incidents?limit=50");
    renderFallbackKg(incidents, services);
    if (!incidents.some(i => (i.affected_services || []).some(s => services.includes(s)))) {
      empty.classList.remove("hidden");
      canvas.classList.add("hidden");
      empty.innerHTML = emptyState(
        "No incident relationships yet",
        "Run an investigation involving one of the selected services and the topology will appear here.",
      );
    }
  } catch (err) {
    empty.classList.remove("hidden");
    canvas.classList.add("hidden");
    toast(err.message, { type: "error", title: "Topology load failed" });
  }
}

function kgColor(label) {
  const l = (label || "").toLowerCase();
  if (l === "service") return "#22d3ee";
  if (l === "incident") return "#fbbf24";
  if (l === "change") return "#34d399";
  if (l === "api" || l === "apiendpoint" || l === "apiroute") return "#a78bfa";
  if (l === "agent") return "#a78bfa";
  return "#8b98a9";
}

function kgRadius(label) {
  const l = (label || "").toLowerCase();
  if (l === "service") return 11;
  if (l === "incident") return 9;
  return 7;
}

function layoutGraph(nodes, edges) {
  const W = 800, H = 420;
  const pos = {};
  const n = nodes.length;
  nodes.forEach((nd, i) => {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    const r = Math.min(W, H) * 0.34;
    pos[nd.id] = { x: W / 2 + Math.cos(a) * r, y: H / 2 + Math.sin(a) * r, vx: 0, vy: 0 };
  });
  const idx = {};
  nodes.forEach((nd, i) => (idx[nd.id] = i));
  const el = edges
    .filter((e) => idx[e.source] != null && idx[e.target] != null)
    .map((e) => [idx[e.source], idx[e.target]]);

  for (let it = 0; it < 260; it++) {
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const a = pos[nodes[i].id], b = pos[nodes[j].id];
      const dx = a.x - b.x, dy = a.y - b.y;
      const d2 = dx * dx + dy * dy + 1e-6, d = Math.sqrt(d2);
      const f = 2600 / d2;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
    for (const [i, j] of el) {
      const a = pos[nodes[i].id], b = pos[nodes[j].id];
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy + 1e-6);
      const f = (d - 105) * 0.05;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
    for (const nd of nodes) {
      const p = pos[nd.id];
      p.vx += (W / 2 - p.x) * 0.012;
      p.vy += (H / 2 - p.y) * 0.012;
      p.vx *= 0.8; p.vy *= 0.8;
      p.x += p.vx; p.y += p.vy;
    }
  }
  return pos;
}

function drawKgElements() {
  const root = $("#kg-svg-root");
  if (!root || !kgState.data) return;
  const { nodes, edges } = kgState.data;
  const pos = kgState.pos;

  const edgeLines = edges
    .filter((e) => pos[e.source] && pos[e.target])
    .map((e) => {
      const a = pos[e.source], b = pos[e.target];
      const rel = e.rel || e.relation || "REL";
      return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"
        stroke="rgba(255,255,255,0.18)" stroke-width="${Math.min(2.5, Math.max(1, Number(e.weight || 1) * 1.6))}">
        <title>${esc(rel)}</title></line>`;
    }).join("");

  const nodeGs = nodes.map((nd) => {
    const p = pos[nd.id] || { x: 400, y: 210 };
    const r = kgRadius(nd.label);
    const c = kgColor(nd.label);
    const props = nd.props ? Object.entries(nd.props).map(([k, v]) => `${esc(k)}=${esc(v)}`).join(" · ") : "";
    return `<g data-node-id="${esc(nd.id)}" transform="translate(${p.x.toFixed(1)},${p.y.toFixed(1)})" style="cursor:pointer">
      <circle r="${r}" fill="rgba(6,14,22,0.92)" stroke="${c}" stroke-width="1.8">
        <title>${esc(nd.key)} · ${esc(nd.label)}${props ? "\n" + props : ""}</title></circle>
      <circle r="${r * 0.42}" fill="${c}" opacity="0.95"></circle>
      <text y="${r + 14}" text-anchor="middle" font-family="var(--mono)" font-size="10.5" font-weight="600" fill="${c}">${esc(nd.key)}</text>
    </g>`;
  }).join("");

  root.innerHTML = edgeLines + nodeGs;
}

function renderKG(data) {
  const nodes = data.nodes || [];
  const edges = data.edges || [];
  const svg = $("#kg-svg");
  const legend = $("#kg-legend");

  if (!nodes.length) {
    $("#kg-empty").classList.remove("hidden");
    $("#kg-canvas").classList.add("hidden");
    return;
  }

  kgState.data = data;
  kgState.pos = layoutGraph(nodes, edges);
  kgState.zoom = 1;
  kgState.panX = 0;
  kgState.panY = 0;

  svg.setAttribute("viewBox", "0 0 800 420");
  svg.innerHTML = '<g id="kg-svg-root"></g>';
  updateKgTransform();
  drawKgElements();

  const labels = [...new Set(nodes.map((nd) => nd.label || "node"))];
  legend.innerHTML = labels.map((l) => `
    <div class="kg-legend-item"><span class="sw" style="background:${kgColor(l)};box-shadow:0 0 8px ${kgColor(l)}"></span>${esc(l)}</div>`).join("");
}

/* ============================================================
   Incident detail drawer
   ============================================================ */
let drawerOpenId = null;

function openIncident(id) {
  drawerOpenId = id;
  $("#drawer-backdrop").classList.add("open");
  $("#incident-drawer").classList.add("open");
  document.body.style.overflow = "hidden";
  $("#drawer-body").innerHTML = `
    <div class="drawer-body">
      <div class="skeleton-rows">${Array(8).fill('<div class="skeleton"></div>').join("")}</div>
    </div>`;
  loadDrawer(id);
}

function closeDrawer() {
  drawerOpenId = null;
  $("#drawer-backdrop").classList.remove("open");
  $("#incident-drawer").classList.remove("open");
  document.body.style.overflow = "";
}

async function loadDrawer(id) {
  const body = $("#drawer-body");
  try {
    const [inc, rc] = await Promise.all([
      api(`/incidents/${id}`),
      api(`/incidents/${id}/root-cause`).catch(() => null),
    ]);
    body.innerHTML = drawerHtml(inc, rc);
    const form = $("#resolve-form");
    if (form) form.addEventListener("submit", (e) => e.preventDefault());
    const btn = $("#resolve-btn");
    if (btn) btn.addEventListener("click", () => submitResolve(id));
  } catch (err) {
    // Check if this is a 404 - incident not found
    const isNotFound = err.message?.includes("not found") || err.status === 404;
    const errorMsg = isNotFound 
      ? "This historical memory entry exists, but the original incident record is no longer available."
      : err.message;
    body.innerHTML = `<div class="drawer-body">${errorState(errorMsg, `()=>closeDrawer()`)}</div>`;
  }
}

function drawerHtml(inc, rc) {
  const resolved = (inc.status || "").toLowerCase() === "resolved";
  const conf = rc ? Number(rc.confidence || 0) : 0;
  const chain = rc?.causal_chain || [];
  const alts = rc ? [...rc.alternatives].sort((a, b) => b.confidence - a.confidence) : [];
  const factors = rc?.contributing_factors || [];

  return `
    <div class="drawer-head">
      <div style="min-width:0">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          ${sevPill(inc.severity)} ${statusPill(inc.status)}
          ${inc.incident_type ? `<span class="pill pill-dim">${esc(inc.incident_type)}</span>` : ""}
        </div>
        <div class="drawer-title" style="margin-top:8px">${esc(inc.title)}</div>
      </div>
      <button class="icon-btn" data-close-drawer aria-label="Close"><svg class="ic"><use href="#i-close"/></svg></button>
    </div>

    <div class="drawer-body">
      <dl class="kv">
        <dt>ID</dt><dd>${monoId(inc.id)}</dd>
        <dt>Services</dt><dd>${chips(inc.affected_services)}</dd>
        <dt>Created</dt><dd class="mono" style="font-size:12px">${fmtDate(inc.created_at)} · ${relTime(inc.created_at)}</dd>
        ${inc.resolved_at ? `<dt>Resolved</dt><dd class="mono" style="font-size:12px">${fmtDate(inc.resolved_at)}</dd>` : ""}
        ${inc.description ? `<dt>About</dt><dd>${esc(inc.description)}</dd>` : ""}
      </dl>

      ${resolved ? `
        <div class="drawer-section"><div class="resolved-banner">
          <svg class="ic" style="width:17px;height:17px"><use href="#i-check"/></svg>
          This incident has been resolved and the resolution fed back into learning.
        </div></div>` : ""}

      <div class="drawer-section">
        <span class="sec-label">Root cause</span>
        ${rc ? `
          <div class="root-cause-box" style="padding:14px 16px">
            <div class="root-cause-text">
              <div class="rc-value" style="font-size:15px">${esc(rc.root_cause || "—")}</div>
            </div>
            <div class="root-cause-meter">
              <div class="conf-meter">
                <div class="conf-top"><span>CONFIDENCE</span><span class="mono" style="color:var(--accent-strong);font-size:15px">${pct(conf)}%</span></div>
                <div class="bar-track">${bar(conf, confCls(conf))}</div>
              </div>
            </div>
          </div>` : `
          <div class="empty-state" style="padding:24px">
            <p>Root cause not yet computed for this incident.</p>
          </div>`}
      </div>

      ${rc ? `
        <div class="drawer-section">
          <span class="sec-label">Causal chain</span>
          ${chain.length ? `<div class="timeline">${chain.map((n) => `
            <div class="timeline-item">
              <span class="timeline-dot"><svg class="ic"><use href="#i-chevron"/></svg></span>
              <div class="timeline-label">${esc(n.label || n.node_id || "—")}</div>
              <div class="timeline-meta">
                <span>node <b>${esc(trunc(n.node_id, 10))}</b></span>
                <span>kind <b>${esc(n.kind || "—")}</b></span>
                <span>conf <b>${pct(n.confidence)}%</b></span>
                ${n.source_agent ? `<span>src <b>${esc(n.source_agent)}</b></span>` : ""}
              </div>
            </div>`).join("")}</div>` : `<p class="hint">No causal chain recorded.</p>`}
        </div>

        <div class="drawer-section">
          <span class="sec-label">Alternative hypotheses</span>
          ${alts.length ? alts.map((a) => `
            <div class="alt-item">
              <div class="alt-cause"><span>${esc(a.cause)}</span><span class="mono" style="color:var(--accent-strong)">${pct(a.confidence)}%</span></div>
              ${bar(a.confidence, confCls(a.confidence))}
            </div>`).join("") : `<p class="hint">None recorded.</p>`}
        </div>

        ${factors.length ? `<div class="drawer-section">
          <span class="sec-label">Contributing factors</span>
          <div class="factor-list">${factors.map((f) => `<span class="chip">${esc(f)}</span>`).join("")}</div>
        </div>` : ""}

        <div class="drawer-section">
          <span class="sec-label">Explanation</span>
          <div class="explanation-text">${esc(rc.explanation || "No explanation available.")}</div>
        </div>` : ""}

      ${resolved ? "" : `
        <div class="drawer-section">
          <span class="sec-label">Resolve incident</span>
          <form class="resolve-form" id="resolve-form">
            <input id="resolve-action" type="text" placeholder="Action taken — e.g. Restarted payment-svc and drained upstream pool" required>
            <textarea id="resolve-steps" rows="3" spellcheck="false" placeholder="Steps (one per line)&#10;1. Scale up worker pool&#10;2. Restart service"></textarea>
            <label class="check-row">
              <input type="checkbox" id="resolve-verified"> 
              <div>
                Verified — resolution confirmed by on-call engineer
                <span class="hint" style="display:block; margin-top:4px; font-weight:normal">**Verified** means the on-call engineer confirmed that the service recovered and the root cause was addressed.</span>
              </div>
            </label>
            <button class="btn btn-primary" type="submit" id="resolve-btn"><svg class="ic" style="width:15px;height:15px"><use href="#i-check"/></svg>Mark resolved</button>
          </form>
        </div>`}
    </div>`;
}

async function submitResolve(id) {
  const action = $("#resolve-action").value.trim();
  const steps = $("#resolve-steps").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const verified = $("#resolve-verified").checked;
  if (!action) { toast("Describe the action taken.", { type: "warn" }); return; }
  const btn = $("#resolve-btn");
  btn.disabled = true;
  try {
    await api(`/incidents/${id}/resolve`, { method: "POST", body: { action, steps, verified } });
    toast("Incident resolved — resolution persisted and fed into learning.", { type: "success", title: "Resolved" });
    state.incidentsDirty = true;
    loadDrawer(id);
    if (currentRoute === "incidents") await loadIncidents();
  } catch (err) {
    toast(err.message, { type: "error", title: "Resolve failed" });
    btn.disabled = false;
  }
}

/* ============================================================
   Side status poller
   ============================================================ */
async function pollStatus() {
  const dot = $("#side-status .status-dot");
  const label = $(".status-label");
  try {
    // Reachability: /health is always unauthenticated.
    const [health, internal, mstats] = await Promise.allSettled([
      api("/health"), api("/internal/health"), api("/memory/stats"),
    ]);
    if (health.status === "fulfilled") {
      dot.className = "status-dot ok";
      label.textContent = "ONLINE";
      dot.title = "API reachable";
    } else {
      dot.className = "status-dot err";
      label.textContent = "OFFLINE";
    }
    // Deep details come from /internal/health when a key is configured.
    const deep = internal.status === "fulfilled" ? internal.value : null;
    $("#ss-env").textContent = deep?.env || (health.status === "fulfilled" && health.value?.env) || "—";
    $("#ss-mem").textContent = deep?.memory_size ?? "—";
    if (mstats.status === "fulfilled") {
      $("#ss-faiss").textContent = mstats.value.faiss_enabled ? "ON" : "OFF";
    }
  } catch (_e) {
    dot.className = "status-dot err";
    label.textContent = "OFFLINE";
  }
}

/* ============================================================
   Global event wiring + init
   ============================================================ */
function wireGlobal() {
  $$(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      navigate(btn.dataset.route);
      closeNav();
    });
  });

  $$("[data-go]").forEach((el) => {
    el.addEventListener("click", () => navigate(el.dataset.go));
  });

  document.addEventListener("click", (e) => {
    const open = e.target.closest("[data-open-incident]");
    if (open) { openIncident(open.dataset.openIncident); return; }
    const row = e.target.closest("tr[data-id]");
    if (row) {
      const interactive = e.target.closest("button, a, input, textarea, select");
      if (!interactive) openIncident(row.dataset.id);
      return;
    }
    if (e.target.closest("[data-close-drawer]")) closeDrawer();
  });

  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeDrawer(); closeNav(); }
  });

  $("#nav-toggle").addEventListener("click", () => {
    $("#sidebar").classList.toggle("open");
    $("#nav-backdrop").classList.toggle("open");
  });
  $("#nav-backdrop").addEventListener("click", closeNav);

  window.addEventListener("hashchange", () => {
    const name = routeFromHash();
    if (name !== currentRoute) activateRoute(name);
  });
}

function closeNav() {
  $("#sidebar").classList.remove("open");
  $("#nav-backdrop").classList.remove("open");
}

function init() {
  wireGlobal();
  initSettings();
  initInvestigate();
  $("#overview-demo")?.addEventListener("click", openDemoInvestigation);
  $("#overview-investigate")?.addEventListener("click", () => navigate("investigate"));
  initIncidents();
  initMemory();
  initPatterns();
  initPredictions();
  initKg();

  const name = routeFromHash();
  if (!location.hash) history.replaceState(null, "", "#/" + name);
  activateRoute(name, { initial: true });

  pollStatus();
  setInterval(pollStatus, 30000);

  if (routeFromHash() === "investigate") { /* nothing */ }
}

document.addEventListener("DOMContentLoaded", init);


/* ---------- Settings / API Key ---------- */
function initSettings() {
  const modal = $("#settings-modal");
  const openBtn = $("#btn-settings");
  const closeBtn = $("#settings-close");
  const saveBtn = $("#settings-save");
  const clearBtn = $("#settings-clear");
  const input = $("#settings-api-key");
  const status = $("#settings-status");
  if (!modal || !openBtn) return;

  const open = () => {
    input.value = getApiKey();
    status.textContent = getApiKey() ? "Advanced API access is configured." : "No advanced API key configured.";
    modal.classList.remove("hidden");
  };
  const close = () => modal.classList.add("hidden");

  openBtn.addEventListener("click", open);
  closeBtn?.addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
  saveBtn?.addEventListener("click", () => {
    setApiKey(input.value.trim());
    status.textContent = "Saved";
    toast("API key saved locally", { type: "success", title: "Settings" });
    setTimeout(close, 600);
  });
  clearBtn?.addEventListener("click", () => {
    setApiKey("");
    input.value = "";
    status.textContent = "Cleared";
    toast("API key cleared", { type: "info" });
  });
}