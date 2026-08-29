/* Trace admin portal — vanilla JS, same-origin only, no external libraries.
   The page shell is public; every data call requires the admin token, sent as
   X-Admin-Token and held only in sessionStorage. Any 403 drops back to the gate. */
"use strict";

const root = document.getElementById("root");
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = {
  token: sessionStorage.getItem("trace_admin_token") || "",
  tab: (location.hash || "#overview").slice(1),
  timers: [],
  prompts: [],            // client-side tail (capped)
  promptSeq: 0,
  promptsPaused: false,
  openPrompt: null,       // seq of the expanded prompt row
  openAccount: null,      // user_id of the expanded account row
};
const clearTimers = () => { S.timers.forEach(clearTimeout); S.timers = []; };
const later = (fn, ms) => S.timers.push(setTimeout(fn, ms));

async function api(path) {
  const r = await fetch(path, { headers: { "X-Admin-Token": S.token } });
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (r.status === 403) {
    const disabled = data && data.error === "admin_disabled";
    S.token = ""; sessionStorage.removeItem("trace_admin_token");
    clearTimers(); renderGate(disabled);
    throw new Error("forbidden");
  }
  return data || {};
}

/* ---------- token gate ---------- */
function renderGate(disabled) {
  root.innerHTML = `
    <div class="gate">
      <div class="gate-card">
        <div class="brand">● Trace <span class="admin-tag">admin</span></div>
        ${disabled
          ? `<p class="err">Admin is not enabled on this server — set the
             <code>TRACE_ADMIN_TOKEN</code> environment variable and redeploy.</p>`
          : `<p class="muted">Paste the admin token to continue. It is kept only in this
             browser tab's session.</p>`}
        <input id="tok" type="password" placeholder="admin token" autocomplete="off">
        <p id="gateMsg" class="err"></p>
        <button id="go">Enter</button>
      </div>
    </div>`;
  const submit = async () => {
    const t = document.getElementById("tok").value.trim();
    if (!t) return;
    S.token = t;
    try {
      await api("/api/admin/overview");
      sessionStorage.setItem("trace_admin_token", t);
      renderShell();
    } catch (e) {
      const m = document.getElementById("gateMsg");
      if (m) m.textContent = "That token was not accepted.";
    }
  };
  document.getElementById("go").onclick = submit;
  document.getElementById("tok").onkeydown = e => { if (e.key === "Enter") submit(); };
}

/* ---------- shell + tabs ---------- */
const TABS = [
  ["overview", "Overview"], ["accounts", "Accounts"], ["pipeline", "Pipeline"],
  ["prompts", "Prompts"], ["architecture", "Architecture"],
];
function renderShell() {
  clearTimers();
  if (!TABS.some(t => t[0] === S.tab)) S.tab = "overview";
  root.innerHTML = `
    <div class="topbar">
      <div class="brand">● Trace <span class="admin-tag">admin</span></div>
      <nav>${TABS.map(([id, label]) =>
        `<a href="#${id}" class="tab ${S.tab === id ? "on" : ""}">${label}</a>`).join("")}</nav>
      <button id="lock" class="linkish">Lock</button>
    </div>
    <div id="body" class="body"><p class="muted">Loading…</p></div>`;
  document.getElementById("lock").onclick = () => {
    S.token = ""; sessionStorage.removeItem("trace_admin_token"); clearTimers(); renderGate();
  };
  ({ overview: renderOverview, accounts: renderAccounts, pipeline: renderPipeline,
     prompts: renderPrompts, architecture: renderArchitecture }[S.tab])();
}
window.addEventListener("hashchange", () => {
  S.tab = (location.hash || "#overview").slice(1);
  if (S.token) renderShell();
});

const badge = (state) => {
  const cls = state === "done" ? "ok" : state === "error" ? "bad" : state === "running" ? "run" : "idle";
  return `<span class="pill ${cls}">${esc(state || "idle")}</span>`;
};

/* ---------- overview ---------- */
async function renderOverview() {
  const d = await api("/api/admin/overview");
  const b = document.getElementById("body");
  if (!b) return;
  const stat = (label, value) => `
    <div class="stat"><div class="num">${esc(value)}</div><div class="lbl">${esc(label)}</div></div>`;
  b.innerHTML = `
    <h2>Platform overview</h2>
    <div class="stats">
      ${stat("accounts", d.users)} ${stat("agents", d.agents)} ${stat("capture sessions", d.sessions)}
      ${stat("prompt log entries", d.prompt_entries)}
    </div>
    <div class="kv">
      <div><span>Database</span><b>${esc(d.db_mode)}</b></div>
      <div><span>Scheduler / deployment sync</span><b>${d.scheduler_alive ? "running" : "not running"}</b></div>
      <div><span>Node mode</span><b>${d.cloud_backend ? "desktop (proxying to cloud)" : "cloud / local"}</b></div>
      <div><span>Signups</span><b>${d.signup_enabled ? "open" : "disabled"}</b></div>
      <div><span>Uptime</span><b>${Math.floor(d.uptime_s / 3600)}h ${Math.floor((d.uptime_s % 3600) / 60)}m</b></div>
    </div>`;
  later(renderOverview, 10000);
}

/* ---------- accounts ---------- */
async function renderAccounts() {
  const d = await api("/api/admin/accounts");
  const b = document.getElementById("body");
  if (!b) return;
  const rows = (d.accounts || []).map(a => {
    const u = a.user || {};
    const open = S.openAccount === u.user_id;
    const agentRows = (a.agents || []).map(g => `
      <div class="subcard">
        <div class="spread"><b>${esc(g.name || g.agent_id)}</b>
          <span class="pill ${g.deployment_id ? "ok" : "idle"}">${g.deployment_id ? "deployment" : "legacy"}</span></div>
        <div class="mono tiny">agent ${esc(g.agent_id)}${g.deployment_id ? ` · ${esc(g.deployment_id)}` : ""}${g.memory_store_id ? ` · mem ${esc(g.memory_store_id)}` : ""}</div>
        <div class="tiny">${g.schedule ? `schedule: ${esc(JSON.stringify(g.schedule.cadence))}${g.schedule.paused ? " (paused)" : ""}` : "no schedule (manual)"}${g.approval_required ? " · staging (approval required)" : " · execution (autonomous)"}</div>
        ${(g.recent_runs || []).slice(0, 3).map(r =>
          `<div class="tiny run"><span class="pill ${r.status === "failed" ? "bad" : "ok"}">${esc(r.status)}</span> ${esc(r.ran_at || "")} — ${esc(r.summary || "")}</div>`).join("")}
      </div>`).join("") || `<p class="muted tiny">No agents.</p>`;
    const sessionRows = (a.sessions || []).map(s => {
      const art = s.artifacts || {};
      const stage = ["raw", "event_log", "discovery", "reviewed", "specs"]
        .map(k => `<span class="dot ${art[k] ? "on" : ""}" title="${k}"></span>`).join("");
      return `<div class="tiny run">${stage} ${esc(s.id)}${s.label ? ` · ${esc(s.label)}` : ""}${s.deploy_at ? " · deployed" : ""}</div>`;
    }).join("") || `<p class="muted tiny">No capture sessions.</p>`;
    return `
      <div class="card acct" data-uid="${esc(u.user_id)}">
        <div class="spread click" data-uid="${esc(u.user_id)}">
          <b>${esc(u.email)}</b>
          <span class="muted tiny">${(a.agents || []).length} agent(s) · ${(a.sessions || []).length} session(s)${a.pending_count ? ` · ${a.pending_count} pending approval(s)` : ""}</span>
        </div>
        <div class="mono tiny muted">${esc(u.user_id)} · ${esc(u.external_user_id)} · created ${esc(u.created_at)}</div>
        ${open ? `<div class="acct-detail"><h4>Agents</h4>${agentRows}<h4>Capture sessions</h4>${sessionRows}</div>` : ""}
      </div>`;
  }).join("");
  b.innerHTML = `<h2>Every account</h2>${rows || `<p class="muted">No accounts yet.</p>`}`;
  b.querySelectorAll(".click").forEach(el => el.onclick = () => {
    S.openAccount = S.openAccount === el.dataset.uid ? null : el.dataset.uid;
    renderAccounts();
  });
}

/* ---------- pipeline (real-time) ---------- */
const STAGE_ORDER = ["reconstruct", "discover", "describe", "specs", "interview",
                     "build", "connect", "deploy"];
async function renderPipeline() {
  const d = await api("/api/admin/pipeline");
  const b = document.getElementById("body");
  if (!b) return;
  const sids = Object.keys(d.sessions || {});
  const cards = sids.map(sid => {
    const meta = (d.meta || {})[sid] || {};
    const stages = d.sessions[sid] || {};
    const rows = STAGE_ORDER.filter(st => stages[st]).map(st => {
      const j = stages[st] || {};
      const prog = (st === "reconstruct" && j.total)
        ? ` <span class="tiny muted">${j.current}/${j.total}</span>` : "";
      return `<div class="stage-row">${badge(j.state)} <b>${st}</b>${prog}
        ${j.error ? `<div class="err tiny">${esc(j.error)}</div>` : ""}</div>`;
    }).join("");
    return `<div class="card">
      <div class="spread"><b class="mono">${esc(sid)}</b>
        <span class="muted tiny">${esc(meta.owner_user_id || "?")}${meta.label ? ` · ${esc(meta.label)}` : ""}</span></div>
      ${rows || `<p class="muted tiny">no jobs recorded</p>`}</div>`;
  }).join("");
  b.innerHTML = `
    <h2>Live pipeline <span class="pulse"></span></h2>
    <p class="muted">Every in-process job on this server, refreshed every 2 seconds.
      ${d.capture ? `Capture in progress: <b class="mono">${esc(d.capture)}</b>` : "No capture in progress."}</p>
    ${cards || `<p class="muted">Nothing is running right now. Start a capture or a describe-workflow and watch it appear here.</p>`}`;
  later(renderPipeline, 2000);
}

/* ---------- prompts (live tail) ---------- */
async function pollPrompts() {
  if (S.promptsPaused) { later(pollPrompts, 2000); return; }
  const d = await api(`/api/admin/prompts?since=${S.promptSeq}`);
  if ((d.entries || []).length) {
    S.prompts.push(...d.entries);
    if (S.prompts.length > 400) S.prompts = S.prompts.slice(-400);
  }
  S.promptSeq = Math.max(S.promptSeq, d.latest_seq || 0);
  drawPrompts();
  later(pollPrompts, 2000);
}
function drawPrompts() {
  const list = document.getElementById("plist");
  if (!list) return;
  list.innerHTML = [...S.prompts].reverse().map(e => {
    const open = S.openPrompt === e.seq;
    return `
    <div class="card prow" data-seq="${e.seq}">
      <div class="spread click-p" data-seq="${e.seq}">
        <span><span class="pill ${e.error ? "bad" : "ok"}">${e.error ? "error" : "ok"}</span>
          <b>${esc(e.engine)}</b> <span class="muted tiny">${esc(e.model)}</span></span>
        <span class="muted tiny">#${e.seq} · ${esc(e.ts)} · ${e.ms}ms${e.sid ? ` · ${esc(e.sid)}` : ""}</span>
      </div>
      ${open ? `
        <h4>System prompt</h4><pre>${esc(e.system)}</pre>
        <h4>User content</h4><pre>${esc(e.user)}</pre>
        <h4>${e.error ? "Error" : "Response"}</h4><pre>${esc(e.error || e.response)}</pre>` : ""}
    </div>`;
  }).join("") || `<p class="muted">No LLM calls yet — run any pipeline step and its exact prompts appear here.</p>`;
  list.querySelectorAll(".click-p").forEach(el => el.onclick = () => {
    const seq = Number(el.dataset.seq);
    S.openPrompt = S.openPrompt === seq ? null : seq;
    drawPrompts();
  });
}
function renderPrompts() {
  const b = document.getElementById("body");
  b.innerHTML = `
    <h2>LLM prompts <span class="pulse"></span></h2>
    <p class="muted">Every prompt this server sends to a model, live (in-memory only — cleared on
      restart; long fields truncated at 20&thinsp;KB). Click a row for the full text.
      <button id="pp" class="linkish">${S.promptsPaused ? "Resume tail" : "Pause tail"}</button></p>
    <div id="plist"></div>`;
  document.getElementById("pp").onclick = () => { S.promptsPaused = !S.promptsPaused; renderPrompts(); };
  drawPrompts();
  pollPrompts();
}

/* ---------- architecture ---------- */
async function renderArchitecture() {
  const d = await api("/api/admin/architecture");
  const b = document.getElementById("body");
  if (!b) return;
  const boxes = (d.components || []).map(c => `
    <div class="node" id="n-${esc(c.id)}">
      <b>${esc(c.label)}</b>
      <div class="tiny muted">${esc(c.detail)}</div>
      ${c.live ? `<div class="tiny live">${Object.entries(c.live).map(([k, v]) => `${esc(k)}: <b>${esc(v)}</b>`).join(" · ")}</div>` : ""}
      ${(c.connects_to || []).length ? `<div class="tiny arrows">→ ${c.connects_to.map(esc).join(" · → ")}</div>` : ""}
    </div>`).join("");
  b.innerHTML = `
    <h2>System architecture</h2>
    <div class="arch">${boxes}</div>
    <h3>Pipeline</h3>
    <div class="flow">${(d.pipeline || []).map(s => `<span class="step">${esc(s)}</span>`).join(`<span class="arrow">→</span>`)}</div>`;
}

/* ---------- boot ---------- */
if (S.token) {
  api("/api/admin/overview").then(renderShell).catch(() => { /* gate already shown */ });
} else {
  renderGate();
}
