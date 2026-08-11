/* ============================================================
   Trace — end-user UI. Direction H aesthetic, wired to the real
   pipeline via /api/auth/* and /api/u/* (scoped to the logged-in
   user). No platform credentials, internal IDs, or file paths are
   ever shown. Stages: record -> find workflows -> review ->
   questions -> build -> connect -> go live.
   ============================================================ */
const $ = (sel, r = document) => r.querySelector(sel);
const root = $("#root");
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const S = { user: null, signupAllowed: true, session: null, conn: null, step: 0, busy: false, _timers: [] };

const STEPS = [
  { key: "record",    label: "Record" },
  { key: "workflows", label: "Find work" },
  { key: "review",    label: "Review" },
  { key: "interview", label: "Questions" },
  { key: "build",     label: "Build" },
  { key: "connect",   label: "Connect" },
  { key: "golive",    label: "Go live" },
];

/* ---------------- API ---------------- */
async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" }, credentials: "same-origin" };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty */ }
  return { ok: r.ok, status: r.status, data: data || {} };
}
const uget = (sid, sub) => api("GET", `/api/u/sessions/${sid}${sub || ""}`);
const upost = (sid, sub, body) => api("POST", `/api/u/sessions/${sid}${sub}`, body || {});

/* Open an external URL. In a normal browser this is a new tab; inside the Tauri
   desktop webview window.open() to an external https URL is blocked, so route
   through the shell's open_external command when present (works after a shell
   rebuild). Either way the caller also shows a copyable link as a guaranteed
   fallback, so connecting works even before the shell supports auto-open. */
function openExternal(url) {
  try {
    const t = window.__TAURI__;
    if (t && t.core && typeof t.core.invoke === "function") {
      t.core.invoke("open_external", { url }).catch(() => window.open(url, "_blank", "noopener"));
      return;
    }
  } catch (e) { /* fall through to a normal browser open */ }
  window.open(url, "_blank", "noopener");
}

function clearTimers() { S._timers.forEach(clearTimeout); S._timers = []; }
function wait(ms) { return new Promise(res => { const t = setTimeout(res, ms); S._timers.push(t); }); }

/* Poll a session job's /status until done|error. onTick(data) renders progress. */
async function pollJob(sid, action, onTick) {
  for (let i = 0; i < 1200; i++) {
    const { data } = await uget(sid, `/${action}/status`);
    if (onTick) onTick(data);
    if (data.state === "done") return data;
    if (data.state === "error") throw new Error(data.error || `${action} failed`);
    await wait(800);
  }
  throw new Error("timed out");
}

/* ---------------- bootstrap ---------------- */
async function boot() {
  const { data } = await api("GET", "/api/auth/me");
  S.signupAllowed = data.signup_enabled !== false;
  S.user = data.user;
  if (!S.user) return renderAuth();
  await loadAndRender();
}

async function refreshSession() {
  const { data } = await api("GET", "/api/u/sessions");
  const list = (data.sessions || []).filter(s => !s.fixture || true);
  S.session = list[0] || null;               // most recent (list is newest-first)
  S.conn = null;
  if (S.session) {
    const c = await uget(S.session.id, "/connection-data");
    if (c.ok) S.conn = c.data;
  }
  S.step = deriveStep();
}

function deriveStep() {
  const s = S.session;
  if (!s) return 0;
  const a = s.artifacts || {};
  if (!a.event_log) return a.raw ? 1 : 0;
  if (!a.discovery) return 1;
  if (!s.reviewed_at) return 2;
  // the questions phase is mandatory before build (already-built sessions keep going)
  if (!s.build_at) return s.interview_complete_at ? 4 : 3;
  if (!connectionUsable()) return 5;
  return 6;
}
function connectionUsable() {
  const apps = (S.conn && S.conn.record && S.conn.record.apps) || {};
  return Object.values(apps).some(a => a.usable);
}
function usableApps() {
  const apps = (S.conn && S.conn.record && S.conn.record.apps) || {};
  return Object.keys(apps).filter(s => apps[s].usable)
    .map(s => ({ slug: s, label: apps[s].label || s }));
}

async function loadAndRender() { await refreshSession(); render(); }

/* ---------------- auth ---------------- */
function renderAuth(mode) {
  mode = mode || "login";
  const isSignup = mode === "signup" && S.signupAllowed;
  root.innerHTML = `
    <div class="topbar"><div class="brand">● Trace</div></div>
    <div class="panel" style="max-width:420px;margin:8vh auto 0;">
      <div class="eyebrow">${isSignup ? "Create account" : "Welcome back"}</div>
      <h2>${isSignup ? "Set up your Trace" : "Sign in"}</h2>
      <p class="lead">${isSignup ? "Your work stays yours — only you can see your agents and connections." : "Pick up where you left off."}</p>
      <div class="field"><span class="label">Email</span><input id="email" type="email" autocomplete="username" placeholder="you@work.com"></div>
      <div class="field"><span class="label">Password</span><input id="pw" type="password" autocomplete="current-password" placeholder="${isSignup ? "at least 8 characters" : "••••••••"}"></div>
      <p id="authMsg" class="msg err"></p>
      <button id="authGo" class="btn-primary full">${isSignup ? "Create account" : "Sign in"}</button>
      ${S.signupAllowed ? `<p class="notice" style="margin-top:14px;text-align:center;">
        ${isSignup ? "Already have an account?" : "New to Trace?"}
        <button id="authSwap" class="btn-link">${isSignup ? "Sign in" : "Create one"}</button></p>` : ""}
    </div>`;
  const go = async () => {
    const email = $("#email").value.trim(), password = $("#pw").value;
    $("#authMsg").textContent = "";
    const { ok, data } = await api("POST", isSignup ? "/api/auth/signup" : "/api/auth/login", { email, password });
    if (!ok) { $("#authMsg").textContent = data.error || "could not sign in"; return; }
    S.user = data.user;
    await loadAndRender();
  };
  $("#authGo").onclick = go;
  $("#pw").onkeydown = (e) => { if (e.key === "Enter") go(); };
  if ($("#authSwap")) $("#authSwap").onclick = () => renderAuth(isSignup ? "login" : "signup");
}

/* ---------------- shell ---------------- */
function shell(inner) {
  const reached = deriveStep();                 // furthest step the artifacts allow
  const chips = STEPS.map((st, i) => {
    const nav = i <= reached;                    // completed steps + the current one are navigable
    const cls = [i === S.step ? "active" : (i < reached ? "done" : ""), nav ? "nav" : ""].join(" ").trim();
    return `<span class="step-chip ${cls}" ${nav ? `data-step="${i}"` : ""}><span class="n">${String(i + 1).padStart(2, "0")}</span>${st.label}</span>`;
  }).join("");
  root.innerHTML = `
    <div class="topbar">
      <div class="brand">● Trace</div>
      <div class="row"><button id="navAgents" class="btn-link">My agents</button>
        <span class="muted" style="font-size:13px;">${esc(S.user.email)}</span>
        <button id="logout" class="btn-link">Sign out</button></div>
    </div>
    <div class="steps">${chips}</div>
    <div id="stage">${inner}</div>`;
  $("#logout").onclick = async () => { await api("POST", "/api/auth/logout", {}); S.user = null; clearTimers(); renderAuth(); };
  $("#navAgents").onclick = renderDashboard;
  root.querySelectorAll(".step-chip.nav").forEach(c => c.onclick = () => goStep(parseInt(c.dataset.step, 10)));
}

function render() {
  clearTimers();
  const fn = [stepRecord, stepWorkflows, stepReview, stepInterview, stepBuild, stepConnect, stepGolive][S.step];
  fn();
}
const goStep = (i) => { S.step = i; render(); };

/* Start a brand-new capture without disturbing existing agents/sessions: null
   the session so deriveStep is 0, then show the record step (Start recording
   creates a fresh dated session server-side). */
function recordAnother() { S.session = null; S.step = 0; render(); }

/* ============ MY AGENTS ============
   Structure: stories = sessions you haven't reviewed yet (approve staged actions
   where applicable; reviewing moves the session into the agent's history log);
   each agent has a profile (bio + controls + session-history log) and a DM thread.
   Trace's own look — normal width, no borrowed chrome. */
const CADENCES = [
  { v: "", label: "manually" },
  { v: "daily", label: "daily · 9am" },
  { v: "weekly", label: "weekly · Mon 9am" },
];
function cadenceToValue(c) {
  if (c && c.kind === "daily") return "daily";
  if (c && c.kind === "weekly") return "weekly";
  return "";
}
function valueToCadence(v) {
  if (v === "daily") return { kind: "daily", time: "09:00" };
  if (v === "weekly") return { kind: "weekly", weekday: 0, time: "09:00" };
  return null;
}
function runBadge(status) {
  const ok = status === "completed" || status === "clean";
  const appr = status === "needs_approval" || status === "halted" || status === "staged";
  const pend = status === "running" || status === "ran";
  const cls = ok ? "pill-on" : (appr ? "pill-warn" : (pend ? "pill" : "pill-off"));
  const label = ok ? "completed" : (appr ? "needs approval"
    : (status === "running" ? "running" : (status === "ran" ? "ran" : "failed")));
  return `<span class="pill ${cls}">${label}</span>`;
}

/* deterministic soft-gradient initial avatar per agent (just an identicon) */
const AV_GRADS = [["#f582ae", "#b8235a"], ["#8bd3dd", "#1f6f8b"], ["#ffd803", "#e16162"],
                  ["#a786df", "#5f2da5"], ["#7ec4a2", "#2e6f4e"], ["#f3a712", "#c1440e"]];
function avHash(s) { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h; }
function avatar(name, size) {
  const g = AV_GRADS[avHash(name || "?") % AV_GRADS.length];
  const initial = (String(name || "?").trim()[0] || "?").toUpperCase();
  return `<span class="ag-av" style="width:${size}px;height:${size}px;font-size:${Math.round(size * .42)}px;
    background:linear-gradient(135deg,${g[0]},${g[1]});">${esc(initial)}</span>`;
}
function ago(iso) {
  const t = Date.parse(iso || "");
  if (isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// one proposed action (checkbox to include in an approve batch; processed = status pill)
function proposalRow(p) {
  const pl = p.payload || {};
  // Long values and nested objects render pre-wrapped (multi-line) so email bodies and
  // row payloads stay readable instead of collapsing to a one-line JSON blob.
  const rows = Object.keys(pl).map(k => {
    const v = pl[k];
    const val = (v && typeof v === "object") ? JSON.stringify(v, null, 2) : String(v);
    const multi = val.length > 60 || val.includes("\n");
    return `<div class="sub" style="margin-top:3px;"><b>${esc(k)}:</b> `
      + (multi ? `<span style="display:block;white-space:pre-wrap;margin-top:1px;">${esc(val)}</span>`
               : esc(val))
      + `</div>`;
  }).join("");
  const done = p.status && p.status !== "pending";
  const why = (p.rationale || "").trim();
  return `<div class="card" style="margin-top:8px;">
    <div class="row" style="gap:8px;align-items:flex-start;">
      ${done ? `<span class="pill ${p.status === "executed" ? "pill-on" : "pill-off"}">${esc(p.status)}</span>`
             : `<input type="checkbox" class="prop" data-id="${esc(p.id)}" checked style="margin-top:4px;">`}
      <div style="flex:1;">
        <div class="title" style="font-size:14px;">${p.app ? `<span class="muted">${esc(p.app)} · </span>` : ""}${esc(p.summary || p.kind || "Action")}</div>
        ${why ? `<div class="sub" style="margin-top:4px;"><span class="muted">Why:</span> ${esc(why)}</div>` : ""}
        <div style="margin-top:4px;">${rows || `<div class="sub muted">No details captured.</div>`}</div>
        ${(!done && p.app) ? `<button class="btn-link prop-autoapprove" data-app="${esc(p.app)}" style="margin-top:6px;font-size:12px;">Auto-approve ${esc(p.app)} from now on</button>` : ""}
      </div>
      ${done ? "" : `<button class="btn-link prop-no" data-id="${esc(p.id)}" style="color:var(--danger,#b3261e);">Reject</button>`}
    </div>
  </div>`;
}

// Per-proposal quick action: stop staging one app (future actions in it run automatically).
// Server computes current-minus-app, so it's race-free. Returns true if the user confirmed.
async function autoApproveApp(agentId, app) {
  if (!confirm(`Stop asking approval for ${app}? Future ${app} actions will run automatically.`)) return false;
  await api("POST", `/api/u/agents/${agentId}/staging`, { unstage: app });
  return true;
}

// Gap #3: a card for a question the agent paused on — context, quick-pick options, and a
// free-text answer. Answering auto-immediately resumes the run.
function decisionCard(d) {
  const opts = (d.options || []).map(o =>
    `<button class="btn-secondary btn-sm dec-opt" data-id="${esc(d.id)}" data-val="${esc(o)}">${esc(o)}</button>`).join(" ");
  return `<div class="card dec-card" style="margin-top:8px;border-left:3px solid var(--accent,#5b8def);">
    <div class="row" style="gap:8px;"><span class="pill pill-warn">needs you</span>
      <div class="title" style="font-size:14px;flex:1;">${esc(d.question || "Needs your input")}</div></div>
    ${d.context ? `<div class="sub" style="margin-top:4px;"><span class="muted">Context:</span> ${esc(d.context)}</div>` : ""}
    ${opts ? `<div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap;">${opts}</div>` : ""}
    <div class="row" style="gap:6px;margin-top:8px;">
      <input type="text" class="dec-answer" data-id="${esc(d.id)}" placeholder="Type your answer…" style="flex:1;">
      <button class="btn-accent btn-sm dec-submit" data-id="${esc(d.id)}">Answer &amp; continue</button>
    </div>
  </div>`;
}

// POST an answer; the server records it and auto-resumes the agent. Returns true on send.
async function answerDecision(id, answer) {
  if (!answer || !answer.trim()) return false;
  await api("POST", `/api/u/decisions/${id}/answer`, { answer: answer.trim() });
  return true;
}

// Phase B: a "confirm this shared lesson?" card — a lesson a SIMILAR agent (possibly
// another user's) learned. Confirming lets it guide this agent autonomously from now on;
// dismissing means it never resurfaces here. This is the one-time human touch per lesson.
function lessonConfirmCard(agentId, c) {
  return `<div class="card lesson-card" style="margin-top:8px;border-left:3px solid var(--accent,#5b8def);">
    <div class="sub muted" style="margin-bottom:4px;">A similar agent learned this — apply it here?</div>
    <div class="title" style="font-size:14px;">${esc(c.lesson || "")}</div>
    <div class="row" style="gap:8px;margin-top:10px;">
      <button class="btn-accent btn-sm lesson-yes" data-agent="${esc(agentId)}" data-id="${esc(c.decision_id)}">Use it</button>
      <button class="btn-link lesson-no" data-agent="${esc(agentId)}" data-id="${esc(c.decision_id)}" style="color:var(--muted,#888);">Dismiss</button>
    </div>
  </div>`;
}

async function loadHome() {
  const { data } = await api("GET", "/api/u/home");
  S.igHome = data || { agents: [], unread_total: 0, needs_you_total: 0 };
  return S.igHome;
}

function agentsTopbar(unread, backFn, needsYou) {
  const ct = (n) => n > 9 ? "9+" : n;
  return {
    html: `<div class="topbar">
      <div class="row">${backFn ? `<button id="agBack" class="btn-link">← back</button>` : ""}
        <div class="brand">● Trace</div></div>
      <div class="row">
        ${needsYou ? `<button id="agNeeds" class="btn-link" style="color:var(--danger,#b3261e);font-weight:600;">Needs you <span class="ct-badge" style="background:var(--danger,#b3261e);">${ct(needsYou)}</span></button>` : ""}
        <button id="agGroups" class="btn-link">Groups</button>
        <button id="agMsgs" class="btn-link">Messages${unread ? ` <span class="ct-badge">${ct(unread)}</span>` : ""}</button>
        <button id="agShare" class="btn-link">Sharing</button>
        <span class="muted" style="font-size:13px;">${esc(S.user.email)}</span>
        <button id="logout" class="btn-link">Sign out</button></div></div>`,
    wire() {
      $("#logout").onclick = async () => { await api("POST", "/api/auth/logout", {}); S.user = null; clearTimers(); renderAuth(); };
      $("#agMsgs").onclick = renderMessages;
      $("#agGroups").onclick = renderGroups;
      $("#agShare").onclick = renderSettings;
      const n = $("#agNeeds");
      if (n) n.onclick = () => { S.igHome = null; renderHome(); };
      const b = $("#agBack");
      if (b) b.onclick = backFn;
    },
  };
}

/* ---------------- Phase B: lesson-sharing consent ---------------- */
const SCOPE_LABELS = {
  contribute: { owner: "Keep to myself", team: "Share with my team", global: "Share with everyone (anonymized)" },
  consume: { owner: "Only my own", team: "My team's", global: "Everyone's (anonymized)" },
};
function scopeSelect(cls, current, kind, includeInherit) {
  const labels = SCOPE_LABELS[kind];
  const opts = (includeInherit ? [["", "Inherit account default"]] : [])
    .concat(["owner", "team", "global"].map(s => [s, labels[s]]));
  return `<select class="${cls}">${opts.map(([v, l]) =>
    `<option value="${v}" ${String(current || "") === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
}

async function renderSettings() {
  clearTimers();
  const home = S.igHome || await loadHome();
  const { data } = await api("GET", "/api/u/consent");
  const c = data || { contribute_scope: "global", consume_scope: "global" };
  const tb = agentsTopbar(home.unread_total, renderHome, home.needs_you_total);
  root.innerHTML = `${tb.html}
    <div class="panel">
      <h2>Lesson sharing</h2>
      <p class="lead">When your agents learn from your decisions, those anonymized lessons can help other similar agents — and theirs can help yours. Choose how much to share and use. This is your account default; each agent can override it on its own page.</p>
      <div class="row" style="gap:10px;margin-top:14px;align-items:center;">
        <span class="label" style="min-width:180px;">Contribute my lessons</span>${scopeSelect("set-contrib", c.contribute_scope, "contribute", false)}</div>
      <div class="row" style="gap:10px;margin-top:8px;align-items:center;">
        <span class="label" style="min-width:180px;">Use others' lessons</span>${scopeSelect("set-consume", c.consume_scope, "consume", false)}</div>
      <button class="btn-accent btn-sm" id="saveConsent" style="margin-top:16px;">Save</button>
      <p class="sub muted" style="margin-top:12px;">"Anonymized" means names, emails, amounts and ids are stripped and a second privacy check runs before anything leaves your account. A shared lesson only ever guides someone else's agent after they confirm it once.</p>
    </div>`;
  tb.wire();
  $("#saveConsent").onclick = async (e) => {
    await api("POST", "/api/u/consent", {
      contribute_scope: $(".set-contrib").value, consume_scope: $(".set-consume").value });
    e.target.textContent = "Saved ✓";
  };
}

/* ---------------- GROUP CHAT: agents as contacts in a group ---------------- */
const colorDot = (c) => `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${esc(c)};margin-right:4px;vertical-align:middle;"></span>`;
// Mirrors registry._AGENT_COLORS — the palette the server falls back to when an agent
// has no explicit color, so the picker offers exactly the same choices.
const AGENT_COLORS = ["#F26A1B", "#2D7FF9", "#12B886", "#E8590C", "#7048E8",
                      "#E64980", "#1098AD", "#F59F00", "#4263EB", "#0CA678"];

async function renderGroups() {
  clearTimers();
  const home = S.igHome || await loadHome();
  const { data } = await api("GET", "/api/u/groups");
  const groups = (data && data.groups) || [];
  const tb = agentsTopbar(home.unread_total, renderHome, home.needs_you_total);
  const groupRows = groups.map(g => `
    <button class="ag-row grp-row" data-id="${esc(g.id)}">
      <span class="ag-mid">
        <span class="title" style="font-size:14.5px;">${esc(g.name)}</span>
        <span class="sub muted">${g.members.map(m => colorDot(m.color) + esc(m.name)).join("  ")}</span>
      </span></button>`).join("");
  const agentOpts = (home.agents || []).map(a =>
    `<label class="row" style="gap:8px;margin-top:4px;cursor:pointer;"><input type="checkbox" class="grp-mem" value="${esc(a.agent_id)}"> ${esc(a.name)}</label>`).join("");
  root.innerHTML = `${tb.html}
    <div class="panel">
      <h2>Group chats</h2>
      <p class="lead">Add your agents to a group and talk to them together. "What did y'all do today?" gets each one's answer; @mention one to give it a task.</p>
      <div class="stack" style="margin-top:12px;">${groupRows || `<p class="muted">No groups yet — make one below.</p>`}</div>
    </div>
    <div class="panel">
      <h2>New group</h2>
      <input type="text" id="grpName" placeholder="Group name (e.g. Operations)" style="width:100%;margin-top:8px;padding:8px;">
      <div style="margin-top:10px;">${agentOpts || `<p class="muted">No agents yet.</p>`}</div>
      <button class="btn-accent btn-sm" id="grpCreate" style="margin-top:12px;">Create group</button>
    </div>`;
  tb.wire();
  root.querySelectorAll(".grp-row").forEach(b => b.onclick = () =>
    renderGroupChat(groups.find(x => x.id === b.dataset.id)));
  $("#grpCreate").onclick = async () => {
    const name = ($("#grpName").value || "").trim();
    if (!name) { $("#grpName").focus(); return; }
    const ids = Array.from(root.querySelectorAll(".grp-mem")).filter(c => c.checked).map(c => c.value);
    await api("POST", "/api/u/groups", { name, agent_ids: ids });
    renderGroups();
  };
}

function renderGroupChat(group) {
  clearTimers();
  if (!group) { renderGroups(); return; }
  const msgs = [];   // {who:'you'|'sys'|agentId, name, color, text, staged}
  const bubble = (m) => m.who === "you"
    ? `<div class="row" style="justify-content:flex-end;margin-top:10px;"><div class="card" style="background:#1A1A1A;color:#fff;max-width:78%;">${esc(m.text)}</div></div>`
    : `<div style="margin-top:10px;">
         <div class="sub" style="color:${esc(m.color)};font-weight:600;">${esc(m.name)}</div>
         <div class="card" style="border-left:3px solid ${esc(m.color)};max-width:82%;">${esc(m.text)}
           ${m.staged ? `<div class="sub muted" style="margin-top:4px;">📋 ${m.staged} action${m.staged > 1 ? "s" : ""} staged — approve on the agent's page.</div>` : ""}
         </div></div>`;
  const draw = () => {
    const tb = agentsTopbar(0, renderGroups, 0);
    root.innerHTML = `${tb.html}
      <div class="panel">
        <div class="row" style="gap:8px;align-items:baseline;"><h2 style="margin:0;">${esc(group.name)}</h2>
          <span class="sub muted">${group.members.map(m => `<span style="color:${esc(m.color)};">${esc(m.name)}</span>`).join(", ") || "no members"}</span></div>
        <div id="grpMsgs" style="margin-top:12px;min-height:140px;max-height:60vh;overflow:auto;">${
          msgs.map(bubble).join("") || `<p class="muted">Try "what did y'all get up to today?" — or @${esc((group.members[0] || {}).name || "agent")} to direct one.</p>`}</div>
        <div class="row" style="gap:6px;margin-top:12px;">
          <input type="text" id="grpInput" placeholder="Message the group…  (@name to direct one)" style="flex:1;padding:8px;">
          <button class="btn-accent btn-sm" id="grpSend">Send</button>
        </div>
      </div>`;
    tb.wire();
    const send = async () => {
      const text = ($("#grpInput").value || "").trim();
      if (!text) return;
      msgs.push({ who: "you", text });
      const btn = $("#grpSend"); if (btn) { btn.disabled = true; btn.textContent = "…"; }
      draw();
      const { status, data } = await api("POST", `/api/u/groups/${group.id}/message`, { text });
      if (status === 429) {
        msgs.push({ who: "sys", name: "Trace", color: "#b3261e",
                    text: (data && data.message) || "Too many messages — give it a moment." });
      } else {
        for (const r of (data && data.replies) || [])
          msgs.push({ who: r.agent_id, name: r.name, color: r.color, text: r.reply,
                      staged: (r.staged || []).length });
      }
      draw();
    };
    $("#grpSend").onclick = send;
    const inp = $("#grpInput");
    if (inp) inp.onkeydown = (e) => { if (e.key === "Enter") send(); };
    const box = $("#grpMsgs"); if (box) box.scrollTop = box.scrollHeight;
  };
  draw();
}

/* ---------------- HOME: stories to review + agents list ---------------- */
async function renderHome() {
  clearTimers();
  const d = await loadHome();
  const agents = d.agents || [];
  const withStories = agents.filter(a => a.unreviewed > 0);
  const stories = withStories.map(a => `
    <button class="st-btn" data-agent="${esc(a.agent_id)}">
      <span class="st-ring">${avatar(a.name, 52)}<span class="ct-badge st-ct">${a.unreviewed}</span></span>
      <span class="st-name">${esc(a.name.length > 14 ? a.name.slice(0, 13) + "…" : a.name)}</span>
    </button>`).join("");
  // Gap #4: "Needs you" — agents with a blocking question or a pending approval, questions
  // first (they block the agent), each deep-linking to the review flow where you resolve it.
  const needsYou = agents.filter(a => a.needs_you > 0)
    .sort((x, y) => (y.open_decisions - x.open_decisions) || (y.needs_you - x.needs_you));
  const nConf = (a) => (a.lesson_confirmations || []).length;
  const needBit = (a) => [
    a.open_decisions ? `${a.open_decisions} question${a.open_decisions > 1 ? "s" : ""}` : "",
    a.pending_approvals ? `${a.pending_approvals} to approve` : "",
    nConf(a) ? `${nConf(a)} to confirm` : ""].filter(Boolean).join(" · ");
  const needRows = needsYou.map(a => `
    <button class="ag-row needs-row" data-agent="${esc(a.agent_id)}" style="border-left:3px solid var(--danger,#b3261e);">
      ${avatar(a.name, 44)}
      <span class="ag-mid">
        <span class="title" style="font-size:14.5px;">${esc(a.name)}</span>
        <span class="sub" style="color:var(--danger,#b3261e);font-weight:600;">${esc(needBit(a))}</span>
      </span>
      <span class="ct-badge" style="background:var(--danger,#b3261e);">${a.needs_you}</span>
    </button>`).join("");
  const rows = agents.map(a => {
    const sched = a.schedule || null;
    const paused = sched && sched.paused;
    const bits = [];
    if (a.open_decisions) bits.push(`${a.open_decisions} awaiting your answer`);
    if (a.pending_approvals) bits.push(`${a.pending_approvals} to approve`);
    if (nConf(a)) bits.push(`${nConf(a)} lesson${nConf(a) > 1 ? "s" : ""} to confirm`);
    if (a.fyi_reviews) bits.push(`${a.fyi_reviews} to review`);
    if (a.unread) bits.push(`${a.unread} new message${a.unread > 1 ? "s" : ""}`);
    bits.push(sched ? (paused ? "schedule paused" : `runs ${cadenceToValue(sched.cadence) || "on its schedule"}`) : "runs when you say");
    return `<button class="ag-row" data-agent="${esc(a.agent_id)}">
      ${avatar(a.name, 44)}
      <span class="ag-mid">
        <span class="row" style="gap:8px;"><span class="title" style="font-size:14.5px;">${esc(a.name)}</span>
          <span class="pill ${paused ? "pill-warn" : "pill-on"}">${paused ? "PAUSED" : "LIVE"}</span></span>
        <span class="ag-bio">${esc(a.description || ("Runs when: " + (a.trigger || "you say so")))}</span>
        <span class="sub muted">${esc(bits.join(" · "))}</span>
      </span>
      ${a.needs_you ? `<span class="ct-badge" style="background:var(--danger,#b3261e);">${a.needs_you}</span>`
        : (a.unreviewed ? `<span class="ct-badge">${a.unreviewed}</span>` : "")}
    </button>`;
  }).join("");
  const tb = agentsTopbar(d.unread_total, null, d.needs_you_total);
  root.innerHTML = `${tb.html}
    <div class="panel">
      <div class="spread"><h2>Your agents</h2>
        <button id="recAnother" class="btn-primary btn-sm">Record another workflow</button></div>
      <p class="lead">Every workflow you've automated. Sessions wait under "To review" until you've seen them — then they move into the agent's history.</p>
      ${needRows ? `<div class="label" style="margin-bottom:8px;color:var(--danger,#b3261e);">Needs you</div>
        <div class="stack" style="margin-bottom:18px;">${needRows}</div>` : ""}
      ${withStories.length ? `<div class="label" style="margin-bottom:8px;">To review</div>
        <div class="st-row">${stories}</div>` : ""}
      <div class="stack" style="margin-top:${withStories.length ? "18" : "0"}px;">
        ${rows || `<p class="muted">No agents yet. Record a workflow to build your first.</p>`}
      </div>
      <button id="backFlow" class="btn-link" style="margin-top:16px;">← back to setup</button>
    </div>`;
  tb.wire();
  $("#recAnother").onclick = recordAnother;
  $("#backFlow").onclick = () => loadAndRender();
  root.querySelectorAll(".st-btn").forEach(b => b.onclick = () => renderReview(b.dataset.agent));
  root.querySelectorAll(".ag-row").forEach(b => b.onclick = () => renderProfile(b.dataset.agent));
  // "Needs you" rows jump to where the work is: the review flow for story questions/
  // approvals, or the profile for lesson-confirmations (which live there).
  root.querySelectorAll(".needs-row").forEach(b => {
    const a = needsYou.find(x => x.agent_id === b.dataset.agent);
    const toReview = a && (a.open_decisions || a.pending_approvals);
    b.onclick = () => toReview ? renderReview(b.dataset.agent) : renderProfile(b.dataset.agent);
  });
  S._timers.push(setTimeout(renderHome, 30000));            // gentle live refresh
}
const renderDashboard = renderHome;                          // legacy entry points

/* ---------------- REVIEW (stories): unreviewed sessions, oldest first ------ */
function renderReview(agentId, startAt) {
  clearTimers();
  const a = ((S.igHome && S.igHome.agents) || []).find(x => x.agent_id === agentId);
  if (!a || !a.stories.length) { renderHome(); return; }
  let i = Math.min(startAt || 0, a.stories.length - 1);
  const close = () => { S.igHome = null; renderHome(); };
  const markReviewed = (st) => api("POST", `/api/u/agents/${agentId}/reviews`, { key: st.key });
  const draw = () => {
    const st = a.stories[i];
    const pending = (st.proposals || []).filter(p => !p.status || p.status === "pending");
    const decs = st.decisions || [];
    const waiting = pending.length || decs.length;
    root.innerHTML = `
      <div class="rv-view">
        <div class="rv-inner">
          <div class="rv-progress">${a.stories.map((_, k) =>
            `<span class="${k < i ? "done" : (k === i ? "cur" : "")}"></span>`).join("")}</div>
          <div class="rv-head">
            ${avatar(a.name, 34)}
            <span><b>${esc(a.name)}</b><span class="muted" style="margin-left:8px;font-size:12.5px;">${esc(st.ran_at || "")}</span></span>
            ${runBadge(st.status)}
            <button class="rv-x" id="rvX">✕</button>
          </div>
          <div class="rv-card">
            <div class="label" style="margin-bottom:8px;">What happened</div>
            <p class="rv-note">${esc(st.note)}</p>
            ${decs.length ? `
              <div class="label" style="margin:16px 0 4px;">Needs your input — answer to continue</div>
              ${decs.map(decisionCard).join("")}
            ` : ""}
            ${st.proposals && st.proposals.length ? `
              <div class="label" style="margin:16px 0 4px;">Proposed actions — approve to run them</div>
              ${st.proposals.map(proposalRow).join("")}
              ${pending.length ? `<button class="btn-accent approve-sel" style="margin-top:12px;">Approve &amp; run selected</button>` : ""}
            ` : ""}
          </div>
          <div class="rv-nav">
            <button class="btn-secondary btn-sm" id="rvPrev" ${i === 0 ? "disabled" : ""}>‹ Previous</button>
            <span class="muted" style="font-size:12.5px;">${i + 1} of ${a.stories.length}</span>
            <button class="btn-primary btn-sm" id="rvNext">${waiting ? "Skip for now ›" : "Mark reviewed ›"}</button>
          </div>
        </div>
      </div>`;
    $("#rvX").onclick = close;
    $("#rvPrev").onclick = () => { if (i > 0) { i--; draw(); } };
    $("#rvNext").onclick = async () => {
      await markReviewed(st);                     // stays a story server-side while pending remain
      if (i < a.stories.length - 1) { i++; draw(); } else { close(); }
    };
    root.querySelectorAll(".approve-sel").forEach(b => b.onclick = async () => {
      const ids = Array.from(root.querySelectorAll(".prop")).filter(c => c.checked).map(c => c.dataset.id);
      if (!ids.length) { b.textContent = "Select at least one"; return; }
      b.disabled = true; b.textContent = "Running…";
      await api("POST", `/api/u/agents/${agentId}/approve-actions`, { pending_ids: ids });
      await markReviewed(st);
      S._timers.push(setTimeout(async () => {     // execution is async; refresh then continue
        await loadHome();
        const fresh = (S.igHome.agents || []).find(x => x.agent_id === agentId);
        if (fresh && fresh.stories.length) renderReview(agentId, Math.min(i, fresh.stories.length - 1));
        else close();
      }, 3500));
    });
    root.querySelectorAll(".prop-no").forEach(b => b.onclick = async () => {
      await api("POST", `/api/u/pending/${b.dataset.id}/reject`, {});
      st.proposals = (st.proposals || []).filter(p => String(p.id) !== String(b.dataset.id));
      draw();
    });
    root.querySelectorAll(".prop-autoapprove").forEach(b => b.onclick = async () => {
      // loosen staging for this app; its still-pending proposals stay approvable as-is
      if (await autoApproveApp(agentId, b.dataset.app)) draw();
    });
    const afterAnswer = () => S._timers.push(setTimeout(async () => {  // resume is async
      await loadHome();
      const fresh = (S.igHome.agents || []).find(x => x.agent_id === agentId);
      if (fresh && fresh.stories.length) renderReview(agentId, Math.min(i, fresh.stories.length - 1));
      else close();
    }, 3500));
    root.querySelectorAll(".dec-opt").forEach(b => b.onclick = async () => {
      if (await answerDecision(b.dataset.id, b.dataset.val)) { await markReviewed(st); afterAnswer(); }
    });
    root.querySelectorAll(".dec-submit").forEach(b => b.onclick = async () => {
      const inp = root.querySelector(`.dec-answer[data-id="${b.dataset.id}"]`);
      if (!inp || !inp.value.trim()) { if (inp) inp.focus(); return; }
      b.disabled = true; b.textContent = "Sending…";
      if (await answerDecision(b.dataset.id, inp.value)) { await markReviewed(st); afterAnswer(); }
    });
  };
  draw();
}

/* ---------------- MESSAGES: chat with your agents ---------------- */
async function renderMessages() {
  clearTimers();
  const d = S.igHome || await loadHome();
  const tb = agentsTopbar(d.unread_total, renderHome, d.needs_you_total);
  root.innerHTML = `${tb.html}
    <div class="panel">
      <h2>Messages</h2>
      <p class="lead">A direct line to each of your agents — ask what it's been doing, or tell it something it should know.</p>
      <div class="stack" id="dmList"><p class="muted">Loading…</p></div>
    </div>`;
  tb.wire();
  const agents = d.agents || [];
  const threads = await Promise.all(agents.map(async a => {
    const r = await api("GET", `/api/u/agents/${a.agent_id}/messages`);
    const msgs = (r.data && r.data.messages) || [];
    return { a, last: msgs[msgs.length - 1] || null };
  }));
  const list = threads.map(({ a, last }) => `
    <button class="ag-row" data-agent="${esc(a.agent_id)}">
      ${avatar(a.name, 44)}
      <span class="ag-mid">
        <span class="title" style="font-size:14.5px;">${esc(a.name)}</span>
        <span class="ag-bio ${a.unread ? "unread" : ""}">${last
          ? esc(((last.sender === "user" ? "You: " : "") + (last.text || "")).slice(0, 72))
          : "No messages yet"}</span>
      </span>
      <span class="sub muted">${last ? esc(ago(last.created_at)) : ""}</span>
      ${a.unread ? `<span class="ct-badge">${a.unread}</span>` : ""}
    </button>`).join("");
  const el = $("#dmList");
  if (el) el.innerHTML = list || `<p class="muted">No agents yet.</p>`;
  root.querySelectorAll(".ag-row").forEach(b => b.onclick = () => renderThread(b.dataset.agent));
}

async function renderThread(agentId) {
  clearTimers();
  await api("POST", `/api/u/agents/${agentId}/messages/read`, {});
  root.innerHTML = `
    <div class="topbar"><div class="row"><button id="agBack" class="btn-link">← messages</button>
      <span id="thHead" class="row" style="gap:8px;"></span></div>
      <button id="logout" class="btn-link">Sign out</button></div>
    <div class="panel dm-panel">
      <div class="dm-chat" id="chat"><p class="muted">Loading…</p></div>
      <div class="dm-bar">
        <input id="dmText" type="text" placeholder="Message…" autocomplete="off">
        <button id="dmSend" class="btn-primary btn-sm">Send</button>
      </div>
    </div>`;
  $("#agBack").onclick = renderMessages;
  $("#logout").onclick = async () => { await api("POST", "/api/auth/logout", {}); S.user = null; clearTimers(); renderAuth(); };
  let name = "";
  const draw = async () => {
    const r = await api("GET", `/api/u/agents/${agentId}/messages`);
    if (!r.ok) { renderMessages(); return null; }
    const msgs = r.data.messages || [];
    name = (r.data.agent && r.data.agent.name) || "";
    const head = $("#thHead");
    if (head) head.innerHTML = `${avatar(name, 26)}<b style="font-size:14px;">${esc(name)}</b>`;
    const chat = $("#chat");
    if (chat) {
      chat.innerHTML = (msgs.map(m => `
        <div class="dm-bubble ${m.sender === "user" ? "me" : "them"}">
          <p>${esc(m.text || "")}</p><span class="dm-time">${esc(ago(m.created_at))}</span>
        </div>`).join("")
        || `<p class="muted" style="text-align:center;padding:30px 10px;">Say hi to ${esc(name || "your agent")}.</p>`)
        + (r.data.replying ? `<div class="dm-bubble them typing"><span></span><span></span><span></span></div>` : "");
      chat.scrollTop = chat.scrollHeight;
    }
    return r.data;
  };
  const poll = async () => {
    const d = await draw();
    if (d && d.replying) S._timers.push(setTimeout(poll, 2000));
  };
  await poll();
  const send = async () => {
    const inp = $("#dmText");
    const text = inp.value.trim();
    if (!text) return;
    inp.value = "";
    const r = await api("POST", `/api/u/agents/${agentId}/messages`, { text });
    if (!r.ok && r.status !== 409) { inp.value = text; return; }
    await poll();
  };
  $("#dmSend").onclick = send;
  $("#dmText").onkeydown = e => { if (e.key === "Enter") send(); };
}

/* ---------------- AGENT PROFILE: bio + controls + session-history log ------ */
async function renderProfile(agentId) {
  clearTimers();
  const home = S.igHome || await loadHome();
  const a = (home.agents || []).find(x => x.agent_id === agentId) || {};
  const name = a.name || agentId;
  const sched = a.schedule || null;
  const cur = cadenceToValue(sched && sched.cadence);
  const paused = sched && sched.paused;
  const opts = CADENCES.map(c => `<option value="${c.v}" ${c.v === cur ? "selected" : ""}>${c.label}</option>`).join("");
  const tb = agentsTopbar(home.unread_total, renderHome, home.needs_you_total);
  root.innerHTML = `${tb.html}
    <div class="panel">
      <div class="row" style="gap:16px;align-items:flex-start;">
        ${avatar(name, 64)}
        <div style="flex:1;">
          <div class="row" style="gap:10px;"><h2 style="margin:0;">${esc(name)}</h2>
            <span class="pill ${paused ? "pill-warn" : "pill-on"}">${paused ? "PAUSED" : "LIVE"}</span></div>
          <p class="ag-bio" style="margin:6px 0 0;">${esc(a.description || ("Runs when: " + (a.trigger || "you say so")))}</p>
          <p class="sub muted" style="margin-top:4px;"><span id="pStats">…</span>${a.deployed_at ? ` · live since ${esc(ago(a.deployed_at))}` : ""}</p>
        </div>
      </div>
      <div class="row" style="margin-top:16px;gap:8px;flex-wrap:wrap;">
        <button class="btn-secondary btn-sm" id="pMsg">Message</button>
        <button class="btn-accent btn-sm" id="pRun">Run now</button>
        <span class="label" style="margin-left:6px;">Runs</span>
        <select class="sched" id="pSched">${opts}</select>
        ${sched ? `<button class="btn-secondary btn-sm" id="pPause">${paused ? "Resume" : "Pause"}</button>` : ""}
      </div>
      <div class="row" style="margin-top:14px;gap:8px;align-items:center;flex-wrap:wrap;">
        <span class="label">Name</span>
        <input id="pName" type="text" value="${esc(a.name || "")}" maxlength="80"
               style="flex:1;min-width:180px;" />
        <button class="btn-secondary btn-sm" id="pNameSave">Save</button>
      </div>
      <div class="row" style="margin-top:10px;gap:8px;align-items:center;flex-wrap:wrap;">
        <span class="label">Color</span>
        ${AGENT_COLORS.map(c => `
          <button class="ag-swatch" data-color="${esc(c)}" title="${esc(c)}"
                  style="width:20px;height:20px;border-radius:50%;background:${esc(c)};
                         border:${c.toLowerCase() === String(a.color || "").toLowerCase()
                                   ? "2px solid var(--fg,#111)" : "1px solid rgba(0,0,0,.15)"};
                         cursor:pointer;padding:0;"></button>`).join("")}
      </div>
      <div class="row" style="margin-top:12px;">
        ${a.capture_present
          ? `<button id="pDelCap" class="btn-link" style="color:var(--danger,#b3261e);">Delete capture data</button>`
          : `<span class="muted" style="font-size:12px;">Capture data deleted</span>`}
      </div>
    </div>
    ${(a.lesson_confirmations && a.lesson_confirmations.length) ? `
    <div class="panel">
      <h2>Shared lessons to confirm</h2>
      <p class="sub muted" style="margin-top:2px;">Lessons a similar agent learned. Confirm one and this agent applies it on its own from then on; dismiss and it won't come back.</p>
      <div style="margin-top:10px;">${a.lesson_confirmations.map(c => lessonConfirmCard(agentId, c)).join("")}</div>
    </div>` : ""}
    ${(a.active_lessons && a.active_lessons.length) ? `
    <div class="panel">
      <h2>Active shared lessons</h2>
      <p class="sub muted" style="margin-top:2px;">Lessons this agent applies automatically. Retire one to stop it.</p>
      <div style="margin-top:10px;">${a.active_lessons.map(c => `
        <div class="card" style="margin-top:8px;">
          <div class="row" style="gap:8px;align-items:flex-start;">
            <div class="title" style="font-size:14px;flex:1;">${esc(c.lesson || "")}</div>
            <button class="btn-link lesson-retire" data-agent="${esc(agentId)}" data-id="${esc(c.decision_id)}" style="color:var(--danger,#b3261e);">Retire</button>
          </div>
        </div>`).join("")}</div>
    </div>` : ""}
    <div class="panel" id="pApprovalsPanel" style="display:none;">
      <h2>Approvals</h2>
      <p class="sub muted" style="margin-top:2px;">Choose which apps need your approval before the agent acts. Others run automatically. Takes effect on the next run.</p>
      <div id="pApprovals" style="margin-top:10px;"></div>
    </div>
    <div class="panel" id="pSharingPanel" style="display:none;">
      <h2>Lesson sharing (this agent)</h2>
      <p class="sub muted" style="margin-top:2px;">Override your account default for just this agent, or leave it to inherit.</p>
      <div id="pSharing" style="margin-top:10px;"></div>
    </div>
    <div class="panel">
      <div class="spread"><h2>Session history</h2>
        ${a.unreviewed ? `<button class="btn-secondary btn-sm" id="pReview">Review ${a.unreviewed} waiting →</button>` : ""}</div>
      <div id="pLog"><p class="muted">Loading…</p></div>
    </div>`;
  tb.wire();
  $("#pMsg").onclick = () => renderThread(agentId);
  const pr = $("#pReview");
  if (pr) pr.onclick = () => renderReview(agentId);
  $("#pRun").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Running…";
    await api("POST", `/api/u/agents/${agentId}/run-now`, {});
    S._timers.push(setTimeout(() => { S.igHome = null; renderProfile(agentId); }, 4000));
  };
  $("#pSched").onchange = async (e) => {
    await api("POST", `/api/u/agents/${agentId}/schedule`, { cadence: valueToCadence(e.target.value) });
    S.igHome = null; renderProfile(agentId);
  };
  const pp = $("#pPause");
  if (pp) pp.onclick = async () => {
    await api("POST", `/api/u/agents/${agentId}/schedule/pause`, { paused: !paused });
    S.igHome = null; renderProfile(agentId);
  };
  const nameSave = $("#pNameSave");
  if (nameSave) nameSave.onclick = async () => {
    const name = ($("#pName").value || "").trim();
    if (!name) return;
    nameSave.disabled = true; nameSave.textContent = "Saving…";
    await api("POST", `/api/u/agents/${agentId}/name`, { name });
    S.igHome = null; renderProfile(agentId);
  };
  document.querySelectorAll(".ag-swatch").forEach(sw => {
    sw.onclick = async () => {
      await api("POST", `/api/u/agents/${agentId}/color`, { color: sw.dataset.color });
      S.igHome = null; renderProfile(agentId);
    };
  });
  const dc = $("#pDelCap");
  if (dc) dc.onclick = async () => {
    if (!confirm("Delete all recorded data for this workflow from your device and the cloud? Your agent keeps running. This can't be undone.")) return;
    dc.disabled = true; dc.textContent = "Deleting…";
    await api("POST", `/api/u/agents/${agentId}/delete-capture-data`, {});
    S.igHome = null; renderProfile(agentId);
  };
  // the log: real per-session activity (summaries + proposals), newest first
  const { data } = await api("GET", `/api/u/agents/${agentId}/activity`);
  const sessionsList = (data && data.activity) || [];
  // Approvals: per-app staged/autonomous toggles (post-deploy, no redeploy). All
  // interpolated values are escaped via esc() (the file's XSS-safe rendering idiom).
  const availApps = (data && data.agent && data.agent.available_apps) || [];
  const apPanel = $("#pApprovalsPanel");
  if (apPanel && availApps.length) {
    apPanel.style.display = "";
    const apMarkup = availApps.map(ap => `
      <label class="row" style="gap:8px;margin-top:6px;cursor:pointer;">
        <input type="checkbox" class="stage-toggle" data-slug="${esc(ap.slug)}" ${ap.staged ? "checked" : ""}>
        <span class="title" style="font-size:14px;">${esc(ap.label)}</span>
        <span class="sub muted">${ap.staged ? "needs approval" : "runs automatically"}</span>
      </label>`).join("")
      + `<button class="btn-accent btn-sm" id="pStageSave" style="margin-top:12px;">Save approval settings</button>`;
    $("#pApprovals").insertAdjacentHTML("beforeend", apMarkup);
    $("#pStageSave").onclick = async (e) => {
      const staged = Array.from(root.querySelectorAll(".stage-toggle"))
        .filter(c => c.checked).map(c => c.dataset.slug);
      if (!staged.length &&
          !confirm("No apps will need approval — this agent will act automatically with no review. Continue?")) return;
      e.target.disabled = true; e.target.textContent = "Saving…";
      await api("POST", `/api/u/agents/${agentId}/staging`, { staged_apps: staged });
      S.igHome = null; renderProfile(agentId);
    };
  }
  // Per-agent lesson-sharing override (Phase B). Empty override = inherit account default.
  const consent = data && data.agent && data.agent.consent;
  const shPanel = $("#pSharingPanel");
  if (shPanel && consent) {
    shPanel.style.display = "";
    const ov = consent.override || {};
    $("#pSharing").insertAdjacentHTML("beforeend", `
      <div class="row" style="gap:10px;align-items:center;">
        <span class="label" style="min-width:170px;">Contribute my lessons</span>${scopeSelect("ag-contrib", ov.contribute_scope, "contribute", true)}</div>
      <div class="row" style="gap:10px;margin-top:8px;align-items:center;">
        <span class="label" style="min-width:170px;">Use others' lessons</span>${scopeSelect("ag-consume", ov.consume_scope, "consume", true)}</div>
      <button class="btn-accent btn-sm" id="pShareSave" style="margin-top:12px;">Save</button>`);
    $("#pShareSave").onclick = async (e) => {
      await api("POST", `/api/u/agents/${agentId}/consent`, {
        contribute_scope: $(".ag-contrib").value || "inherit",
        consume_scope: $(".ag-consume").value || "inherit" });
      e.target.textContent = "Saved ✓";
    };
  }
  const stats = $("#pStats");
  if (stats) {
    const staged = sessionsList.reduce((n, s) =>
      n + ((s.proposals || []).filter(p => !p.status || p.status === "pending").length), 0);
    stats.textContent = `${sessionsList.length} session${sessionsList.length === 1 ? "" : "s"}`
      + (staged ? ` · ${staged} staged action${staged === 1 ? "" : "s"} waiting` : "");
  }
  const logRow = (s) => {
    const props = s.proposals || [];
    const decs = s.decisions || [];
    const approvable = props.filter(p => !p.status || p.status === "pending");
    return `<div class="log-row" data-sess="${esc(s.run_session_id || "")}">
      <div class="row" style="gap:10px;">
        <span class="mono muted" style="font-size:12px;white-space:nowrap;">${esc(s.ran_at || "")}</span>
        ${runBadge(s.status)}
      </div>
      <div class="log-note">${esc(s.summary || "No summary yet — the run may still be in progress.")}</div>
      ${decs.length ? `<div style="margin-top:8px;"><div class="label">Needs your input</div>
        ${decs.map(decisionCard).join("")}
      </div>` : ""}
      ${props.length ? `<div style="margin-top:8px;"><div class="label">Proposed actions</div>
        ${props.map(proposalRow).join("")}
        ${approvable.length ? `<button class="btn-accent btn-sm approve-sel" style="margin-top:10px;">Approve &amp; run selected</button>` : ""}
      </div>` : ""}
    </div>`;
  };
  const log = $("#pLog");
  if (log) log.innerHTML = sessionsList.length
    ? sessionsList.map(logRow).join("")
    : `<p class="muted">No sessions yet — hit Run now or set a schedule.</p>`;
  root.querySelectorAll(".approve-sel").forEach(b => b.onclick = async () => {
    const panel = b.closest("[data-sess]");
    const ids = Array.from(panel.querySelectorAll(".prop")).filter(c => c.checked).map(c => c.dataset.id);
    if (!ids.length) { b.textContent = "Select at least one"; return; }
    b.disabled = true; b.textContent = "Running…";
    await api("POST", `/api/u/agents/${agentId}/approve-actions`, { pending_ids: ids });
    S._timers.push(setTimeout(() => { S.igHome = null; renderProfile(agentId); }, 4000));
  });
  root.querySelectorAll(".prop-no").forEach(b => b.onclick = async () => {
    await api("POST", `/api/u/pending/${b.dataset.id}/reject`, {});
    S.igHome = null; renderProfile(agentId);
  });
  root.querySelectorAll(".prop-autoapprove").forEach(b => b.onclick = async () => {
    if (await autoApproveApp(agentId, b.dataset.app)) { S.igHome = null; renderProfile(agentId); }
  });
  const refreshAfterAnswer = () =>
    S._timers.push(setTimeout(() => { S.igHome = null; renderProfile(agentId); }, 4000));
  root.querySelectorAll(".dec-opt").forEach(b => b.onclick = async () => {
    if (await answerDecision(b.dataset.id, b.dataset.val)) refreshAfterAnswer();
  });
  root.querySelectorAll(".dec-submit").forEach(b => b.onclick = async () => {
    const inp = root.querySelector(`.dec-answer[data-id="${b.dataset.id}"]`);
    if (!inp || !inp.value.trim()) { if (inp) inp.focus(); return; }
    b.disabled = true; b.textContent = "Sending…";
    if (await answerDecision(b.dataset.id, inp.value)) refreshAfterAnswer();
  });
  const confirmLesson = async (agent, id, action) => {
    await api("POST", "/api/u/lessons/confirm", { agent_id: agent, decision_id: id, action });
    S.igHome = null; renderProfile(agentId);
  };
  root.querySelectorAll(".lesson-yes").forEach(b => b.onclick = () => confirmLesson(b.dataset.agent, b.dataset.id, "confirm"));
  root.querySelectorAll(".lesson-no").forEach(b => b.onclick = () => confirmLesson(b.dataset.agent, b.dataset.id, "dismiss"));
  root.querySelectorAll(".lesson-retire").forEach(b => b.onclick = () => {
    if (confirm("Retire this lesson? This agent will stop applying it.")) confirmLesson(b.dataset.agent, b.dataset.id, "retire");
  });
}
const renderActivity = renderProfile;                        // legacy entry point

/* ---------------- 01 RECORD ---------------- */
function stepRecord() {
  shell(`
    <div class="panel">
      <div class="eyebrow">Step one</div>
      <h2>Record your day</h2>
      <p class="lead">Trace watches how you work for a while and learns your routines. On your Mac · no screenshots · sensitive text removed before saving.</p>
      <div id="recBody"></div>
    </div>`);
  renderRecBody("idle");
}
function renderRecBody(mode, started) {
  const b = $("#recBody");
  if (mode === "recording") {
    b.innerHTML = `
      <div class="card center" style="padding:26px;">
        <div class="row" style="gap:8px;"><span class="rec-dot"></span><span class="label" style="color:var(--accent);">● Recording</span></div>
        <div class="big-timer" id="recTime">00:00:00</div>
        <div class="waveform">${Array.from({length: 16}).map((_, i) =>
          `<b style="height:${10 + (i % 4) * 8}px;background:${i % 2 ? "var(--accent)" : "var(--accent-track)"};"></b>`).join("")}</div>
      </div>
      <button id="recStop" class="btn-secondary full" style="margin-top:14px;">Stop recording</button>`;
    const t0 = started || Date.now();
    const tick = () => {
      const s = Math.floor((Date.now() - t0) / 1000);
      const hh = String(Math.floor(s / 3600)).padStart(2, "0");
      const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
      const ss = String(s % 60).padStart(2, "0");
      if ($("#recTime")) { $("#recTime").textContent = `${hh}:${mm}:${ss}`; S._timers.push(setTimeout(tick, 1000)); }
    };
    tick();
    $("#recStop").onclick = async () => {
      $("#recStop").disabled = true; $("#recStop").textContent = "Saving…";
      await api("POST", "/api/u/capture/stop", {});
      await loadAndRender();
    };
    return;
  }
  if (mode === "describe") {
    b.innerHTML = `
      <div class="field">
        <span class="label">Describe your workflow</span>
        <textarea id="descText" rows="7" placeholder="In a few sentences: what kicks this work off, the steps you take, the apps you use — name them (e.g. Gmail, Google Sheets) — and what the finished result looks like."></textarea>
      </div>
      <p class="notice">The more concretely you name your apps and steps, the better your agent will be. You'll review and correct what Trace understood next.</p>
      <p id="recMsg" class="msg err"></p>
      <div class="row" style="gap:10px;">
        <button id="descBack" class="btn-secondary">← Back</button>
        <button id="descGo" class="btn-primary" style="flex:1;">Continue</button>
      </div>`;
    $("#descBack").onclick = () => renderRecBody("idle");
    $("#descGo").onclick = async () => {
      const text = $("#descText").value.trim();
      if (!text) { $("#recMsg").textContent = friendly("empty_description"); return; }
      $("#descGo").disabled = true; $("#descGo").textContent = "Reading your description…";
      const { ok, data } = await api("POST", "/api/u/describe-workflow", { description: text });
      if (!ok) {
        $("#recMsg").textContent = friendly(data.error || "could not read the description");
        $("#descGo").disabled = false; $("#descGo").textContent = "Continue";
        return;
      }
      try {
        await pollJob(data.session_id, "describe");
        await loadAndRender();                 // lands on Review (event_log + discovery exist)
      } catch (e) {
        $("#recMsg").textContent = friendly(e.message);
        $("#descGo").disabled = false; $("#descGo").textContent = "Try again";
      }
    };
    return;
  }
  b.innerHTML = `
    <div class="row" style="gap:10px;">
      <button id="recStart" class="btn-primary">Start recording</button>
      <span class="muted" style="font-size:13px;">or</span>
      <button id="recSample" class="btn-secondary">Use a sample day</button>
    </div>
    <div class="row" style="margin-top:10px;">
      <button id="recDescribe" class="btn-link">Skip recording — describe it instead</button>
    </div>
    <p id="recMsg" class="msg err" style="margin-top:12px;"></p>`;
  $("#recStart").onclick = async () => {
    const { ok, data } = await api("POST", "/api/u/capture/start", {});
    if (!ok) { $("#recMsg").textContent = data.error === "already_running" ? "a recording is already in progress" : (data.detail || data.error || "could not start"); return; }
    renderRecBody("recording", Date.now());
  };
  $("#recSample").onclick = async () => {
    $("#recSample").disabled = true;
    const fx = await api("GET", "/api/u/fixtures");
    const name = (fx.data.fixtures && fx.data.fixtures[0] && fx.data.fixtures[0].name);
    if (!name) { $("#recMsg").textContent = "no sample available"; $("#recSample").disabled = false; return; }
    const { ok, data } = await api("POST", "/api/u/load-fixture", { name });
    if (!ok) { $("#recMsg").textContent = data.error || "could not load sample"; $("#recSample").disabled = false; return; }
    await loadAndRender();
  };
  $("#recDescribe").onclick = () => renderRecBody("describe");
}

/* ---------------- 02 FIND WORKFLOWS (reconstruct + discover) ---------------- */
function stepWorkflows() {
  const sid = S.session.id, a = S.session.artifacts || {};
  shell(`
    <div class="panel">
      <div class="eyebrow">Step two</div>
      <h2>Finding your routines</h2>
      <p class="lead">Trace reads back your day and groups the repeating work it can take off your plate.</p>
      <div class="progress"><i id="wfBar" style="width:0%"></i></div>
      <p id="wfMsg" class="msg">${a.event_log ? "Looking for repeatable workflows…" : "Reading your day…"}</p>
      <div id="wfErr" class="msg err"></div>
    </div>`);
  runWorkflows(sid, a);
}
async function runWorkflows(sid, a) {
  const bar = $("#wfBar"), msg = $("#wfMsg");
  try {
    if (!a.event_log) {
      msg.textContent = "Reading your day…";
      const r = await upost(sid, "/reconstruct", { force: true });
      if (r.status === 409) { /* short recording -> proceed */ await upost(sid, "/reconstruct", { force: true }); }
      await pollJob(sid, "reconstruct", d => { if (d.total) bar.style.width = Math.round(40 * (d.current || 0) / d.total) + "%"; });
    }
    bar.style.width = "45%";
    msg.textContent = "Looking for repeatable workflows…";
    await upost(sid, "/discover", {});
    await pollJob(sid, "discover", () => { bar.style.width = "80%"; });
    bar.style.width = "100%";
    await loadAndRender();
  } catch (e) {
    $("#wfErr").textContent = friendly(e.message);
  }
}

/* ---------------- 03 REVIEW & APPROVE ---------------- */
async function stepReview() {
  const sid = S.session.id;
  shell(`<div class="panel"><div class="eyebrow">Step three</div><h2>Review what we found</h2>
    <p class="lead">Here's what we think each workflow is. Check our description of each one — confirm it, or fix it in your own words. Your words win. What you mark <b>Automate</b> gets built; <b>Manual</b> stays yours to handle.</p>
    <div id="revBody"><p class="muted">Loading…</p></div></div>`);
  const { ok, data } = await uget(sid, "/review-data");
  if (!ok) { $("#revBody").innerHTML = `<p class="msg err">${friendly(data.error)}</p>`; return; }
  const reviewed = data.reviewed;
  // One flat list; remember each item's original + current classification so the
  // user can flip in either direction (Spec 5). The backend routes by classification.
  const items = [...(reviewed.bundles || []), ...(reviewed.anti_bundles || [])];
  const orig = {}, cls = {};
  items.forEach(it => { orig[it.id] = it.classification; cls[it.id] = it.classification; });
  // Forced approve/correct of the understanding: every card must be confirmed or
  // corrected (non-empty text) before Continue enables. Rehydrates from a prior save.
  const und = {}, corr = {};
  items.forEach(it => {
    und[it.id] = (it.understanding === "confirmed" || it.understanding === "corrected") ? it.understanding : "";
    corr[it.id] = it.user_description || "";
  });
  const resolved = (id) => und[id] === "confirmed" || (und[id] === "corrected" && corr[id].trim());
  // the one-line reason the engine gave (fall back to the internal "why" fields)
  const reasonOf = (it) => it.rationale || it.why_automatable || it.why_judgment || "";
  // plain-language summary of what Trace thinks this workflow is (anti_bundles
  // have no trigger/steps; they carry why_judgment instead)
  const summary = (it) => {
    const rows = [];
    if (it.description) rows.push(`<div class="sub">${esc(it.description)}</div>`);
    if (it.trigger) rows.push(`<div class="sub"><b>Starts when:</b> ${esc(it.trigger)}</div>`);
    if ((it.steps || []).length)
      rows.push(`<ol class="sub" style="margin:6px 0 0 18px;">${it.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>`);
    if (it.why_judgment) rows.push(`<div class="sub"><b>Why we'd leave it to you:</b> ${esc(it.why_judgment)}</div>`);
    if (!rows.length) rows.push(`<div class="sub muted">${esc(reasonOf(it) || "no description")}</div>`);
    return rows.join("");
  };

  const card = (it) => {
    const id = it.id, auto = cls[id] === "bundle";
    const reason = reasonOf(it);
    return `<div class="card" data-id="${esc(id)}" style="display:block;">
      <div class="spread">
        <div class="title">${esc(it.name || id)}</div>
        <span class="pill ${auto ? "pill-on" : "pill-off"}">${auto ? "AUTOMATE" : "MANUAL"}</span>
      </div>
      <div class="sub" style="margin-top:6px;">${reason ? esc(reason) : `<span class="muted">no reason given</span>`}</div>
      <div class="sub" style="margin-top:10px;"><b>What we think this workflow is</b></div>
      ${summary(it)}
      <div class="row" style="margin-top:10px;gap:6px;">
        <button class="und ${und[id] === "confirmed" ? "btn-accent" : "btn-secondary"} btn-sm" data-id="${esc(id)}" data-act="confirmed">Looks right</button>
        <button class="und ${und[id] === "corrected" ? "btn-accent" : "btn-secondary"} btn-sm" data-id="${esc(id)}" data-act="corrected">Not quite — fix it</button>
      </div>
      ${und[id] === "corrected" ? `<div class="field" style="margin-top:8px;">
        <textarea class="corr" data-id="${esc(id)}" rows="3" placeholder="In a line or two: what actually happens — what kicks it off, what you do, and where the result ends up.">${esc(corr[id])}</textarea>
      </div>` : ""}
      <div class="row" style="margin-top:10px;gap:6px;">
        <button class="flip ${auto ? "btn-accent" : "btn-secondary"} btn-sm" data-id="${esc(id)}" data-to="bundle">Automate</button>
        <button class="flip ${auto ? "btn-secondary" : "btn-accent"} btn-sm" data-id="${esc(id)}" data-to="anti_bundle">Manual</button>
      </div>
    </div>`;
  };

  const gate = () => {
    const left = items.filter(it => !resolved(it.id)).length;
    const n = items.filter(it => cls[it.id] === "bundle").length;
    $("#revGo").disabled = !!left;
    $("#revGo").textContent = left
      ? `Confirm or fix ${left} more`
      : (n ? `Continue with ${n} to automate` : "Continue");
  };

  const paint = () => {
    $("#revStack").innerHTML = items.length
      ? items.map(card).join("") : `<p class="muted">Nothing to review.</p>`;
    root.querySelectorAll(".flip").forEach(b => b.onclick = () => {
      cls[b.dataset.id] = b.dataset.to; paint();
    });
    root.querySelectorAll(".und").forEach(b => b.onclick = () => {
      und[b.dataset.id] = b.dataset.act; paint();
    });
    // update state + gating without repainting, so typing is never interrupted
    root.querySelectorAll(".corr").forEach(t => t.oninput = () => {
      corr[t.dataset.id] = t.value; gate();
    });
    gate();
  };

  $("#revBody").innerHTML = `
    <div class="stack" id="revStack"></div>
    <p id="revMsg" class="msg err" style="margin-top:14px;"></p>
    <button id="revGo" class="btn-primary full" style="margin-top:6px;">Continue</button>`;
  paint();

  $("#revGo").onclick = async () => {
    // Apply the user's authoritative classification, preserving each item's fields.
    const outBundles = [], outAnti = [];
    items.forEach(it => {
      const id = it.id, finalCls = cls[id], changed = finalCls !== orig[id];
      const o = { ...it, classification: finalCls, reclassified_from: changed ? orig[id] : null };
      // the forced approve/correct outcome; never send stale text with a confirm
      // (the backend coerces any non-empty user_description to "corrected")
      o.understanding = und[id];
      o.user_description = und[id] === "corrected" ? corr[id].trim() : "";
      if (finalCls === "bundle") {
        o.disposition = "approve_for_build";
        if (orig[id] === "anti_bundle")            // carry the prior reason for the record
          o.original_reason_for_judgment = it.why_judgment || it.rationale || "";
        outBundles.push(o);
      } else {
        outAnti.push(o);                           // manual → excluded from building
      }
    });
    $("#revGo").disabled = true; $("#revGo").textContent = "Saving…";
    const r = await upost(sid, "/reviewed-bundles",
                          { bundles: outBundles, anti_bundles: outAnti, notes: reviewed.notes || "" });
    if (!r.ok) { $("#revMsg").textContent = friendly(r.data.error); $("#revGo").disabled = false; return; }
    await refreshSession(); goStep(3);
  };
}

/* ---------------- 04 INTERVIEW ---------------- */
async function stepInterview() {
  const sid = S.session.id;
  shell(`<div class="panel"><div class="eyebrow">Step four</div><h2>A few quick questions</h2>
    <p class="lead">Things your screen didn't show us. Answer each in a line — not sure? Telling the agent what to do when it's unsure (even "use your best judgment and flag it") is a perfectly good answer.</p>
    <div id="ivBody"><p class="muted">Preparing questions…</p></div></div>`);
  // Generate when the server hasn't finished a questions round for the current
  // review (interview_at is set at generation end and cleared on re-review).
  if (!(S.session && S.session.interview_at)) {
    try {
      const r = await upost(sid, "/interview", {});
      if (!r.ok && r.status !== 409) {        // 409 = already running -> just poll
        throw new Error((r.data && r.data.error) || "couldn't prepare the questions");
      }
      await pollJob(sid, "interview");
      await refreshSession();                 // pick up interview_at / completeness
    } catch (e) {
      $("#ivBody").innerHTML = `<p class="msg err">${friendly(e.message)}</p>
        <button id="ivRetry" class="btn-secondary" style="margin-top:10px;">Try again</button>`;
      $("#ivRetry").onclick = () => stepInterview();
      return;
    }
  }
  const { data } = await uget(sid, "/interview-data");
  renderInterview(sid, data);
}
function renderInterview(sid, data) {
  const interviews = data.interviews || {};
  const ids = data.approved_ids || Object.keys(interviews);
  const blocks = ids.map(bid => {
    const iv = interviews[bid] || { questions: [] };
    const prior = {}; (iv.answers || []).forEach(a => prior[a.q] = a.answer);
    const qs = (iv.questions || []).map(q => `
      <div class="field">
        <div class="title" style="margin-bottom:8px;">${esc(q.question)}</div>
        <input type="text" class="iv-a" data-bid="${esc(bid)}" data-q="${esc(q.id)}"
          value="${esc(prior[q.id] || "")}" placeholder="one line — every question needs an answer">
        ${(q.suggested_answers || []).length ? `<div class="chips" style="margin-top:8px;">${
          q.suggested_answers.map(s => `<button class="suggest" data-q="${esc(q.id)}" data-bid="${esc(bid)}">${esc(s)}</button>`).join("")}</div>` : ""}
      </div>`).join("");
    if (!iv.questions || !iv.questions.length) return "";
    return `<div class="card" style="padding:16px;"><div class="label" style="margin-bottom:10px;">${esc(data.names && data.names[bid] || bid)}</div>${qs}</div>`;
  }).join("");
  $("#ivBody").innerHTML = `
    ${blocks || `<p class="muted">No questions needed — you're good to go.</p>`}
    <p id="ivMsg" class="msg ok" style="margin-top:12px;"></p>
    <div class="row" style="margin-top:6px;gap:10px;">
      <button id="ivGo" class="btn-primary" style="flex:1;">Save & continue</button>
    </div>`;
  // mandatory: continue only once EVERY question has a non-empty answer
  const updateGo = () => {
    const inputs = Array.from(root.querySelectorAll(".iv-a"));
    $("#ivGo").disabled = inputs.some(inp => !inp.value.trim());
  };
  root.querySelectorAll(".iv-a").forEach(inp => inp.oninput = updateGo);
  root.querySelectorAll(".suggest").forEach(btn => btn.onclick = () => {
    const inp = root.querySelector(`.iv-a[data-bid="${btn.dataset.bid}"][data-q="${btn.dataset.q}"]`);
    if (inp) { inp.value = btn.textContent; updateGo(); }
  });
  updateGo();
  const saveAll = async () => {
    const byB = {};
    root.querySelectorAll(".iv-a").forEach(inp => {
      const v = inp.value.trim(); if (!v) return;
      (byB[inp.dataset.bid] = byB[inp.dataset.bid] || {})[inp.dataset.q] = v;
    });
    for (const bid of Object.keys(byB)) {
      const r = await upost(sid, `/interview/${bid}`, { answers: byB[bid] });
      if (!r.ok || (r.data && r.data.status && r.data.status !== "complete")) {
        throw new Error((r.data && r.data.error) || "answers didn't save — try again");
      }
    }
  };
  $("#ivGo").onclick = async () => {
    $("#ivGo").disabled = true; $("#ivGo").textContent = "Saving…";
    try {
      await saveAll();
      await refreshSession();                 // pick up interview_complete_at
      goStep(4);
    } catch (e) {
      const m = $("#ivMsg"); m.className = "msg err"; m.textContent = friendly(e.message);
      $("#ivGo").textContent = "Save & continue"; updateGo();
    }
  };
}

/* ---------------- 05 BUILD ---------------- */
function stepBuild() {
  const sid = S.session.id;
  shell(`<div class="panel"><div class="eyebrow">Step five</div><h2>Building your agents</h2>
    <p class="lead">Turning your routines and answers into working agents.</p>
    <div id="buildBody"></div></div>`);
  runBuild(sid);
}
async function runBuild(sid) {
  const b = $("#buildBody");
  // already built?
  if (S.session.build_at) { b.innerHTML = builtSummary(); wireBuiltContinue(); return; }
  b.innerHTML = `<div class="card"><div class="row"><span class="spinner"></span>
    <span class="title">Building…</span></div></div><div id="buildErr" class="msg err" style="margin-top:12px;"></div>`;
  try {
    const r = await upost(sid, "/build", {});
    if (!r.ok && r.status !== 409) {          // 409 = already running -> just poll
      throw new Error((r.data && r.data.error) || "build failed to start");
    }
    await pollJob(sid, "build");
    await refreshSession();
    b.innerHTML = builtSummary(); wireBuiltContinue();
  } catch (e) { $("#buildErr").textContent = friendly(e.message); }
}
function builtSummary() {
  return `
    <div class="card center" style="padding:22px;">
      <div class="big-check">✓</div>
      <div class="title" style="font-size:17px;">Your agents are built</div>
      <div class="notice">Next, connect the apps they'll work in.</div>
    </div>
    <button id="buildGo" class="btn-primary full" style="margin-top:14px;">Connect your apps</button>`;
}
function wireBuiltContinue() { $("#buildGo").onclick = () => goStep(5); }

/* ---------------- 06 CONNECT ---------------- */
async function stepConnect() {
  const sid = S.session.id;
  shell(`<div class="panel"><div class="eyebrow">Step six</div><h2>Connect your apps</h2>
    <p class="lead">Sign in with each service. Trace never sees your password, and only uses accounts that match your work email.</p>
    <div id="connBody"><p class="muted">Loading…</p></div></div>`);
  await renderConnect(sid);
}
async function renderConnect(sid) {
  const { data } = await uget(sid, "/connection-data");
  S.conn = data;
  const rec = data.record || { apps: {} };
  const email = rec.expected_identity || "";
  const apps = rec.apps || {};
  const slugs = Object.keys(apps);
  const appCard = (slug) => {
    const a = apps[slug];
    let status = `<span class="pill pill-off">not connected</span>`;
    let action = `<button class="btn-primary btn-sm conn-go" data-slug="${esc(slug)}">Connect</button>`;
    if (a.usable) { status = `<span class="pill pill-on">● connected</span>`; action = ""; }
    else if (a.connected && a.flag) { status = `<span class="pill pill-warn">check email</span>`; }
    return `<div class="card spread"><div><div class="title">${esc(a.label || slug)}</div>
      ${a.flag ? `<div class="sub" style="color:#b06a14;">${esc(a.flag)}</div>` : ""}</div>
      <div class="row">${status}${action}</div></div>`;
  };
  $("#connBody").innerHTML = `
    <div class="field"><span class="label">Your work email</span>
      <div class="row"><input id="connEmail" type="email" value="${esc(email)}" placeholder="the account your agents act as">
        <button id="connEmailGo" class="btn-secondary btn-sm">Confirm</button></div></div>
    <p id="connMsg" class="msg"></p>
    <div class="stack" style="margin-top:8px;">${slugs.map(appCard).join("") || `<p class="muted">No apps to connect for these routines.</p>`}</div>
    <button id="connContinue" class="btn-primary full" style="margin-top:16px;" ${connectionUsable() ? "" : "disabled"}>Continue</button>`;
  $("#connEmailGo").onclick = async () => {
    const e = $("#connEmail").value.trim();
    const r = await upost(sid, "/connection-identity", { email: e });
    $("#connMsg").className = "msg " + (r.ok ? "ok" : "err");
    $("#connMsg").textContent = r.ok ? "email confirmed" : friendly(r.data.error);
  };
  root.querySelectorAll(".conn-go").forEach(btn => btn.onclick = () => connectApp(sid, btn.dataset.slug));
  $("#connContinue").onclick = () => goStep(6);
}
async function connectApp(sid, slug) {
  $("#connMsg").className = "msg"; $("#connMsg").textContent = "opening sign-in…";
  await upost(sid, `/connect/${slug}`, {});
  let url = null;
  try {
    const d = await pollJob(sid, "connect", () => {});
    url = d.connect_url;
  } catch (e) { $("#connMsg").textContent = friendly(e.message); return; }
  if (url) {
    openExternal(url);
    $("#connMsg").innerHTML =
      `Approve <b>${esc(slug)}</b> in your browser, then click verify below.` +
      `<div class="muted" style="margin-top:6px;font-size:13px;">No window? Copy this link into your browser:</div>` +
      `<div class="row" style="margin-top:4px;gap:6px;">` +
        `<input id="connUrl" readonly value="${esc(url)}" style="flex:1;font-size:12px;" onfocus="this.select()">` +
        `<button id="connCopy" class="btn-secondary btn-sm" type="button">Copy</button>` +
      `</div>`;
    const cp = $("#connCopy");
    if (cp) cp.onclick = () => {
      const i = $("#connUrl"); i.focus(); i.select();
      try { document.execCommand("copy"); cp.textContent = "Copied"; } catch (e) { /* user can copy manually */ }
    };
  } else {
    $("#connMsg").textContent = `Could not start sign-in for ${slug}. Please try again.`;
  }
  $("#connBody").insertAdjacentHTML("beforeend",
    `<button id="connVerify" class="btn-accent full" style="margin-top:10px;">I've approved — verify</button>`);
  $("#connVerify").onclick = async () => {
    $("#connVerify").disabled = true; $("#connVerify").textContent = "Verifying…";
    await upost(sid, `/connect/${slug}/callback`, {});
    try { await pollJob(sid, "connect", () => {}); } catch (e) { /* status carries result */ }
    await renderConnect(sid);
  };
}

/* ---------------- 07 GO LIVE ---------------- */
async function stepGolive() {
  const sid = S.session.id;
  shell(`<div class="panel"><div class="eyebrow">Step seven</div><h2>${S.session.deploy_at ? "Your agents are working" : "You're all set"}</h2>
    <p class="lead">Your agents will handle this work on their own from now on, starting on the next matching task.</p>
    <div id="liveBody"><p class="muted">Loading…</p></div></div>`);
  await renderGolive(sid);
}
async function renderGolive(sid) {
  if (!S.session.deploy_at) {
    if (!S.conn) { try { S.conn = (await uget(sid, "/connection-data")).data; } catch (e) { /* best effort */ } }
    const staging = usableApps();
    const stageRow = (a) => `<label class="row" style="gap:8px;justify-content:flex-start;margin-top:8px;">
        <input type="checkbox" class="stage-app" data-slug="${esc(a.slug)}" checked>
        <span class="title" style="font-size:13.5px;">Stage ${esc(a.label)} actions for my approval</span></label>`;
    const stagingHtml = staging.length ? `
      <div class="card" style="margin-top:12px;text-align:left;">
        <div class="title" style="font-size:14px;">Before they act</div>
        <div class="sub" style="margin-top:4px;">For anything you'd rather check first, keep it staged: the agent prepares the action and you approve it here before it actually happens. Unchecked apps run automatically.</div>
        ${staging.map(stageRow).join("")}
      </div>` : "";
    $("#liveBody").innerHTML = `
      <div class="card center" style="padding:22px;"><div class="big-check">↑</div>
        <div class="title" style="font-size:17px;">Ready to go live</div>
        <div class="notice">They'll start on the next matching task.</div></div>
      ${stagingHtml}
      <p id="liveMsg" class="msg err" style="margin-top:12px;"></p>
      <button id="liveGo" class="btn-primary full" style="margin-top:6px;">Go live</button>`;
    $("#liveGo").onclick = async () => {
      $("#liveGo").disabled = true; $("#liveGo").textContent = "Deploying…";
      const staged_apps = Array.from(document.querySelectorAll(".stage-app"))
        .filter(c => c.checked).map(c => c.dataset.slug);
      const r = await upost(sid, "/deploy", { trigger: "on a new matching task", staged_apps });
      if (!r.ok && r.status !== 202) {
        $("#liveGo").disabled = false; $("#liveGo").textContent = "Go live";
        if (r.data.error === "not_eligible") {
          $("#liveMsg").className = "msg err";
          $("#liveMsg").textContent = "Connect an app your agents can act in first.";
          if (!$("#liveBack"))
            $("#liveMsg").insertAdjacentHTML("afterend",
              `<button id="liveBack" class="btn-secondary full" style="margin-top:10px;">Back to Connect</button>`);
          $("#liveBack").onclick = () => goStep(5);
        } else {
          $("#liveMsg").className = "msg err"; $("#liveMsg").textContent = friendly(r.data.error);
        }
        return;
      }
      try { await pollJob(sid, "deploy"); } catch (e) { $("#liveMsg").textContent = friendly(e.message); }
      await refreshSession(); render();
    };
    return;
  }
  const { data } = await uget(sid, "/deploy-bundles");
  const bundles = data.bundles || [];
  const held = (b) => (b.held_actions || []).map(a => {
    if (a.status !== "held") return `<div class="sub" style="margin-top:6px;">${esc(a.summary)} — ${esc(a.status)}</div>`;
    return `<div class="row" style="margin-top:8px;gap:8px;"><span class="title" style="font-size:13.5px;flex:1;">${esc(a.summary)}</span>
      <button class="btn-accent btn-sm dep-ok" data-b="${esc(b.id)}" data-a="${esc(a.action_id)}">Approve</button>
      <button class="btn-secondary btn-sm dep-no" data-b="${esc(b.id)}" data-a="${esc(a.action_id)}">Decline</button></div>`;
  }).join("");
  // a bundle with no deploy record (creation failed) must NOT show as LIVE
  const statusPill = (b) => b.status === "live"
    ? `<span class="pill pill-on">LIVE</span>`
    : `<span class="pill pill-off">${esc((b.status || "not created").toUpperCase())}</span>`;
  $("#liveBody").innerHTML = `
    <div class="stack">${bundles.map(b => `<div class="card"><div class="spread">
      <div class="row"><span class="dot"></span><span class="title">${esc(b.name || b.id)}</span></div>
      ${statusPill(b)}</div>${held(b)}</div>`).join("")
      || `<p class="muted">No agents are live yet.</p>`}</div>
    <p id="liveMsg" class="msg"></p>
    <div class="row" style="margin-top:8px;gap:8px;">
      <button id="liveRefresh" class="btn-secondary" style="flex:1;">Refresh</button>
      <button id="recAnother" class="btn-primary" style="flex:1;">Record another workflow</button></div>
    <button id="navAgents2" class="btn-link" style="margin-top:10px;">See all my agents →</button>`;
  const resolve = async (bid, aid, decision) => {
    await upost(sid, `/deploy/${bid}/${decision}`, { action_id: aid });
    await renderGolive(sid);
  };
  root.querySelectorAll(".dep-ok").forEach(b => b.onclick = () => resolve(b.dataset.b, b.dataset.a, "approve"));
  root.querySelectorAll(".dep-no").forEach(b => b.onclick = () => resolve(b.dataset.b, b.dataset.a, "reject"));
  $("#liveRefresh").onclick = () => renderGolive(sid);
  $("#recAnother").onclick = recordAnother;
  $("#navAgents2").onclick = renderDashboard;
}

/* ---------------- helpers ---------------- */
function friendly(err) {
  const e = String(err || "").toLowerCase();
  if (e.includes("no_api_key")) return "the service isn't configured yet — please contact your administrator";
  if (e.includes("not_eligible")) return "finish the previous step first";
  if (e.includes("auth_required")) return "please sign in again";
  if (e.includes("interview_incomplete")) return "please answer the remaining questions first";
  if (e.includes("empty_description")) return "tell us a bit about the work first";
  return err || "something went wrong";
}

boot();
