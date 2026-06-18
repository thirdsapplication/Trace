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
  if (!s.build_at) return 3;                 // interview (optional) then build live at step 4
  if (!connectionUsable()) return 5;
  return 6;
}
function connectionUsable() {
  const apps = (S.conn && S.conn.record && S.conn.record.apps) || {};
  return Object.values(apps).some(a => a.usable);
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

/* ---------------- AGENTS DASHBOARD (Spec 2/3/4) ---------------- */
const CADENCES = [
  { v: "", label: "manually" },
  { v: "interval:15", label: "every 15 min" },
  { v: "hourly", label: "hourly" },
  { v: "daily", label: "daily · 9am" },
];
function cadenceToValue(c) {
  if (!c) return "";
  if (c.kind === "interval") return `interval:${c.minutes || 15}`;
  if (c.kind === "hourly") return "hourly";
  if (c.kind === "daily") return "daily";
  return "";
}
function valueToCadence(v) {
  if (!v) return null;
  if (v.startsWith("interval:")) return { kind: "interval", minutes: parseInt(v.split(":")[1], 10) };
  if (v === "hourly") return { kind: "hourly" };
  if (v === "daily") return { kind: "daily", time: "09:00" };
  return null;
}
function runBadge(status) {
  // completed/needs_approval/failed (+ tolerate older clean/halted records)
  const ok = status === "completed" || status === "clean";
  const appr = status === "needs_approval" || status === "halted";
  const cls = ok ? "pill-on" : (appr ? "pill-warn" : "pill-off");
  const label = ok ? "completed" : (appr ? "needs approval" : "failed");
  return `<span class="pill ${cls}">${label}</span>`;
}
function agentCard(a) {
  const sched = a.schedule || null;
  const cur = cadenceToValue(sched && sched.cadence);
  const paused = sched && sched.paused;
  const runs = a.recent_runs || [];
  const last = runs[0];
  const opts = CADENCES.map(c => `<option value="${c.v}" ${c.v === cur ? "selected" : ""}>${c.label}</option>`).join("");
  return `<div class="card" data-agent="${esc(a.agent_id)}">
    <div class="spread">
      <div class="row"><span class="dot"></span><span class="title">${esc(a.name || a.bundle_id || a.agent_id)}</span></div>
      <span class="pill ${paused ? "pill-warn" : "pill-on"}">${paused ? "PAUSED" : "LIVE"}</span></div>
    <div class="row" style="margin-top:10px;gap:8px;align-items:center;flex-wrap:wrap;">
      <span class="label">Runs</span>
      <select class="sched" data-agent="${esc(a.agent_id)}">${opts}</select>
      ${sched ? `<button class="btn-secondary btn-sm pausebtn" data-agent="${esc(a.agent_id)}">${paused ? "Resume" : "Pause"}</button>` : ""}
      <button class="btn-accent btn-sm runnow" data-agent="${esc(a.agent_id)}">Run now</button>
    </div>
    <details class="agent-activity" style="margin-top:10px;" ${last ? "open" : ""}>
      <summary class="label" style="cursor:pointer;">Activity${runs.length ? ` (${runs.length})` : ""}</summary>
      ${runs.length ? runs.map(r => {
        const acts = r.details || [];
        return `
        <div class="run-row" style="margin-top:8px;">
          <div class="row" style="gap:8px;align-items:center;">
            <span class="muted" style="font-size:12px;">${esc(r.ran_at || "")}</span>${runBadge(r.status)}
            ${acts.length ? `<span class="muted" style="font-size:12px;">${acts.length} action${acts.length === 1 ? "" : "s"}</span>` : ""}
          </div>
          <div class="sub" style="margin-top:2px;">${esc(r.summary || "")}</div>
          ${acts.length ? `<details style="margin-top:4px;"><summary class="muted" style="font-size:12px;cursor:pointer;">what it did</summary>
            <ul style="margin:6px 0 0 16px;padding:0;">${acts.map(d => `<li class="sub">${esc(d)}</li>`).join("")}</ul></details>` : ""}
        </div>`; }).join("")
        : `<div class="muted" style="font-size:13px;margin-top:6px;">No runs yet — set a schedule or hit Run now.</div>`}
    </details>
  </div>`;
}

async function renderDashboard() {
  clearTimers();
  const { data } = await api("GET", "/api/u/agents");
  const agents = (data && data.agents) || [];
  root.innerHTML = `
    <div class="topbar"><div class="brand">● Trace</div>
      <div class="row"><span class="muted" style="font-size:13px;">${esc(S.user.email)}</span>
        <button id="logout" class="btn-link">Sign out</button></div></div>
    <div class="panel">
      <div class="spread"><h2>Your agents</h2>
        <button id="recAnother" class="btn-primary btn-sm">Record another workflow</button></div>
      <p class="lead">Every workflow you've automated. Record another any time — new agents are added without touching the ones you already have.</p>
      <div class="stack" id="agentList">${agents.length ? agents.map(agentCard).join("")
        : `<p class="muted">No agents yet. Record a workflow to build your first.</p>`}</div>
      <button id="backFlow" class="btn-link" style="margin-top:16px;">← back to setup</button>
    </div>`;
  $("#logout").onclick = async () => { await api("POST", "/api/auth/logout", {}); S.user = null; clearTimers(); renderAuth(); };
  $("#recAnother").onclick = recordAnother;
  $("#backFlow").onclick = () => loadAndRender();
  // Spec 4: schedule / pause / run-now per agent
  root.querySelectorAll("select.sched").forEach(sel => sel.onchange = async () => {
    await api("POST", `/api/u/agents/${sel.dataset.agent}/schedule`,
              { cadence: valueToCadence(sel.value) });
    renderDashboard();
  });
  root.querySelectorAll(".pausebtn").forEach(b => b.onclick = async () => {
    await api("POST", `/api/u/agents/${b.dataset.agent}/schedule/pause`,
              { paused: b.textContent === "Pause" });
    renderDashboard();
  });
  root.querySelectorAll(".runnow").forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "Running…";
    await api("POST", `/api/u/agents/${b.dataset.agent}/run-now`, {});
    // the run is async; refresh shortly to show the recorded outcome
    setTimeout(renderDashboard, 4000);
  });
}

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
  b.innerHTML = `
    <div class="row" style="gap:10px;">
      <button id="recStart" class="btn-primary">Start recording</button>
      <span class="muted" style="font-size:13px;">or</span>
      <button id="recSample" class="btn-secondary">Use a sample day</button>
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
    <p class="lead">Here's what we saw and why. If we got one wrong, flip it — what you mark <b>Automate</b> gets built; <b>Manual</b> stays yours to handle.</p>
    <div id="revBody"><p class="muted">Loading…</p></div></div>`);
  const { ok, data } = await uget(sid, "/review-data");
  if (!ok) { $("#revBody").innerHTML = `<p class="msg err">${friendly(data.error)}</p>`; return; }
  const reviewed = data.reviewed;
  // One flat list; remember each item's original + current classification so the
  // user can flip in either direction (Spec 5). The backend routes by classification.
  const items = [...(reviewed.bundles || []), ...(reviewed.anti_bundles || [])];
  const orig = {}, cls = {};
  items.forEach(it => { orig[it.id] = it.classification; cls[it.id] = it.classification; });
  // the one-line reason the engine gave (fall back to the internal "why" fields)
  const reasonOf = (it) => it.rationale || it.why_automatable || it.why_judgment || "";

  const card = (it) => {
    const id = it.id, auto = cls[id] === "bundle";
    const reason = reasonOf(it);
    return `<div class="card" data-id="${esc(id)}" style="display:block;">
      <div class="spread">
        <div class="title">${esc(it.name || id)}</div>
        <span class="pill ${auto ? "pill-on" : "pill-off"}">${auto ? "AUTOMATE" : "MANUAL"}</span>
      </div>
      <div class="sub" style="margin-top:6px;">${reason ? esc(reason) : `<span class="muted">no reason given</span>`}</div>
      <div class="row" style="margin-top:10px;gap:6px;">
        <button class="flip ${auto ? "btn-accent" : "btn-secondary"} btn-sm" data-id="${esc(id)}" data-to="bundle">Automate</button>
        <button class="flip ${auto ? "btn-secondary" : "btn-accent"} btn-sm" data-id="${esc(id)}" data-to="anti_bundle">Manual</button>
      </div>
    </div>`;
  };

  const paint = () => {
    $("#revStack").innerHTML = items.length
      ? items.map(card).join("") : `<p class="muted">Nothing to review.</p>`;
    root.querySelectorAll(".flip").forEach(b => b.onclick = () => {
      cls[b.dataset.id] = b.dataset.to; paint();
    });
    const n = items.filter(it => cls[it.id] === "bundle").length;
    $("#revGo").textContent = n ? `Continue with ${n} to automate` : "Continue";
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
    <p class="lead">Things your screen didn't show us — answer in a line, or skip any you're unsure about.</p>
    <div id="ivBody"><p class="muted">Preparing questions…</p></div></div>`);
  // generate if not yet present
  const data0 = await uget(sid, "/interview-data");
  const have = data0.ok && Object.keys(data0.data.interviews || {}).length;
  if (!have) {
    try {
      await upost(sid, "/interview", {});
      await pollJob(sid, "interview");
    } catch (e) { $("#ivBody").innerHTML = `<p class="msg err">${friendly(e.message)}</p>`; return; }
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
          value="${esc(prior[q.id] || "")}" placeholder="one line — or leave blank to skip">
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
      <button id="ivSkip" class="btn-secondary">Skip</button>
      <button id="ivGo" class="btn-primary" style="flex:1;">Save & continue</button>
    </div>`;
  root.querySelectorAll(".suggest").forEach(btn => btn.onclick = () => {
    const inp = root.querySelector(`.iv-a[data-bid="${btn.dataset.bid}"][data-q="${btn.dataset.q}"]`);
    if (inp) inp.value = btn.textContent;
  });
  const saveAll = async () => {
    const byB = {};
    root.querySelectorAll(".iv-a").forEach(inp => {
      const v = inp.value.trim(); if (!v) return;
      (byB[inp.dataset.bid] = byB[inp.dataset.bid] || {})[inp.dataset.q] = v;
    });
    for (const bid of Object.keys(byB)) await upost(sid, `/interview/${bid}`, { answers: byB[bid] });
  };
  $("#ivGo").onclick = async () => { $("#ivGo").disabled = true; $("#ivGo").textContent = "Saving…"; await saveAll(); goStep(4); };
  $("#ivSkip").onclick = () => goStep(4);
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
    await upost(sid, "/build", {});
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
    $("#liveBody").innerHTML = `
      <div class="card center" style="padding:22px;"><div class="big-check">↑</div>
        <div class="title" style="font-size:17px;">Ready to go live</div>
        <div class="notice">They'll start on the next matching task.</div></div>
      <p id="liveMsg" class="msg err" style="margin-top:12px;"></p>
      <button id="liveGo" class="btn-primary full" style="margin-top:6px;">Go live</button>`;
    $("#liveGo").onclick = async () => {
      $("#liveGo").disabled = true; $("#liveGo").textContent = "Deploying…";
      const r = await upost(sid, "/deploy", { trigger: "on a new matching task" });
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
  $("#liveBody").innerHTML = `
    <div class="stack">${bundles.map(b => `<div class="card"><div class="spread">
      <div class="row"><span class="dot"></span><span class="title">${esc(b.name || b.id)}</span></div>
      <span class="pill pill-on">${esc((b.status || "live").toUpperCase())}</span></div>${held(b)}</div>`).join("")
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
  return err || "something went wrong";
}

boot();
