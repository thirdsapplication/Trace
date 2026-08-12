/* ============================================================
   Palza — end-user UI, wired to the real
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

/* ---------------- brand ---------------- */
// The "routes" mark (PALZA_REDESIGN §1): five colored routes converging on an ink dot
// with a white center. Static — used in the lockup wherever the brand appears.
function logoMark(w = 32, h = 26) {
  return `<svg width="${w}" height="${h}" viewBox="0 0 110 90" fill="none" aria-hidden="true">
    <path d="M6 9 C52 9 52 45 82 45"  stroke="#FF6655" stroke-width="10" stroke-linecap="round"/>
    <path d="M6 27 C48 27 48 45 82 45" stroke="#FFC83D" stroke-width="10" stroke-linecap="round"/>
    <path d="M6 45 H82"                stroke="#315EF6" stroke-width="10" stroke-linecap="round"/>
    <path d="M6 63 C48 63 48 45 82 45" stroke="#45B86B" stroke-width="10" stroke-linecap="round"/>
    <path d="M6 81 C52 81 52 45 82 45" stroke="#A58AE8" stroke-width="10" stroke-linecap="round"/>
    <circle cx="90" cy="45" r="14" fill="#121A2F"/><circle cx="90" cy="45" r="5.5" fill="#FFFFFF"/>
  </svg>`;
}
// Mark + wordmark, the lockup used in the topbar today and the sidebar from step 3.
function brandLockup() {
  return `<span class="brand">${logoMark(28, 23)}<span>palza</span></span>`;
}
// The animated splash (frame 02): FOUR curved routes drawing in toward a blue hub that
// pops, then the wordmark rises. Deliberately not the five-route static mark — frame 02
// is the source of truth for this screen. Keyframes live in app.css.
function splashMark() {
  const routes = [
    ["M20 15 C104 15 104 75 164 75", "#FF6655", "0s"],
    ["M20 45 C96 45 96 75 164 75", "#FFC83D", ".12s"],
    ["M20 105 C96 105 96 75 164 75", "#45B86B", ".24s"],
    ["M20 135 C104 135 104 75 164 75", "#A58AE8", ".36s"],
  ];
  return `<svg width="220" height="150" viewBox="0 0 220 150" fill="none" aria-hidden="true">
    ${routes.map(([d, c, delay]) => `<path d="${d}" stroke="${c}" stroke-width="12"
      stroke-linecap="round" stroke-dasharray="170"
      style="animation:palzaDraw 4.2s ease-in-out ${delay} infinite"/>`).join("")}
    <circle cx="178" cy="75" r="19" fill="#315EF6"
      style="transform-box:fill-box;transform-origin:center;animation:palzaPop 4.2s ease-in-out infinite"/>
  </svg>`;
}
// Shown once per launch, then cross-fades to whatever comes next. Resolves after the
// animation's meaningful beats (draw ~1.3s, pop ~1.9s, wordmark ~2.1s) rather than the
// full 4.2s loop, which would idle on a blank hold before repeating.
// The splash is already on screen (it ships in index.html so it paints on the first
// frame). It stays up until the first screen is ready rather than for a fixed duration —
// a timer either cuts off a slow load or stalls a fast one. A short floor stops it
// flashing when everything is warm.
const SPLASH_FLOOR_MS = 500;
const _splashAt = Date.now();
function hideSplash() {
  const el = document.getElementById("splash");
  if (!el) return;
  const wait = Math.max(0, SPLASH_FLOOR_MS - (Date.now() - _splashAt));
  setTimeout(() => { el.classList.add("splash-out"); setTimeout(() => el.remove(), 320); }, wait);
}

async function boot() {
  const { data } = await api("GET", "/api/auth/me");
  S.signupAllowed = data.signup_enabled !== false;
  S.user = data.user;
  if (!S.user) { renderAuth(); return hideSplash(); }
  const fr = await api("GET", "/api/u/first-run");
  if (!(fr.data && fr.data.terms_accepted_at)) { renderWelcome(); return hideSplash(); }
  if (fr.data.first_run_completed_at) { S.igHome = null; await renderHome(); return hideSplash(); }
  await loadAndRender();
  hideSplash();
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


/* ---------------- first run (04, 05) ---------------- */
// A new account is walked through Welcome → Privacy & terms before the wizard; a
// returning account lands on Home and only re-enters the wizard via "+ New workflow".
const CORE_DOTS = ["#FF6655", "#FFC83D", "#45B86B", "#A58AE8", "#315EF6"];

function renderWelcome() {
  root.className = "wrap";
  root.innerHTML = `
    <div class="panel first-run">
      ${logoMark(96, 78)}
      <h1 class="fr-title">Meet your new teammates.</h1>
      <p class="fr-lead">Palza watches one workday, finds the work that repeats, and builds
        an agent for each. Nothing leaves without your approval.</p>
      <div class="row" style="gap:8px;">${CORE_DOTS.map(c =>
        `<span class="fr-dot" style="background:${c};"></span>`).join("")}</div>
      <button id="frGo" class="btn-accent" style="padding:13px 34px;margin-top:6px;">Get started</button>
    </div>`;
  $("#frGo").onclick = renderTerms;
}

function renderTerms() {
  root.className = "wrap";
  const points = [
    ["#FF6655", "Recording is opt-in, always visible",
     "Capture only runs during a session you start, with a live indicator you can stop anytime."],
    ["#45B86B", "Your capture data stays yours",
     "Used only to build your agents. Delete it any time from any agent's settings."],
    ["#315EF6", "Nothing leaves without approval",
     "Every outbound email, edit, or upload is staged for you to approve first."],
  ];
  root.innerHTML = `
    <div class="panel first-run" style="align-items:stretch;text-align:left;">
      <h1 class="fr-title" style="font-size:26px;">Before we begin</h1>
      <p class="fr-lead" style="max-width:none;">Palza is built on recording your work.
        Here is exactly how that is handled.</p>
      ${points.map(([c, t, d]) => `<div class="fr-point">
        <span class="fr-bullet" style="background:${c};"></span>
        <div><div class="fr-point-t">${t}</div><div class="fr-point-d">${d}</div></div>
      </div>`).join("")}
      <label class="row" style="gap:10px;margin-top:4px;cursor:pointer;">
        <input type="checkbox" id="frAgree">
        <span style="font-size:12.5px;color:var(--ink-2);">I agree to the terms of service and privacy policy</span>
      </label>
      <button id="frAccept" class="btn-accent full" disabled>Agree &amp; continue</button>
    </div>`;
  $("#frAgree").onchange = (e) => { $("#frAccept").disabled = !e.target.checked; };
  $("#frAccept").onclick = async () => {
    $("#frAccept").disabled = true;
    await api("POST", "/api/u/first-run/terms", {});
    await api("POST", "/api/u/first-run/complete", {});
    await loadAndRender();                       // straight into the wizard at Record
  };
}

/* ---------------- auth ---------------- */
function renderAuth(mode) {
  mode = mode || "login";
  const isSignup = mode === "signup" && S.signupAllowed;
  root.innerHTML = `
    <div class="topbar">${brandLockup()}</div>
    <div class="panel" style="max-width:420px;margin:8vh auto 0;">
      <div class="eyebrow">${isSignup ? "Create account" : "Welcome back"}</div>
      <h2>${isSignup ? "Set up your palza" : "Sign in"}</h2>
      <p class="lead">${isSignup ? "Your work stays yours — only you can see your agents and connections." : "Pick up where you left off."}</p>
      <div class="field"><span class="label">Email</span><input id="email" type="email" autocomplete="username" placeholder="you@work.com"></div>
      <div class="field"><span class="label">Password</span><input id="pw" type="password" autocomplete="current-password" placeholder="${isSignup ? "at least 8 characters" : "••••••••"}"></div>
      <p id="authMsg" class="msg err"></p>
      <button id="authGo" class="btn-primary full">${isSignup ? "Create account" : "Sign in"}</button>
      ${S.signupAllowed ? `<p class="notice" style="margin-top:14px;text-align:center;">
        ${isSignup ? "Already have an account?" : "New to palza?"}
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
  // The app shell clears root's .wrap class because the sidebar owns layout. The wizard
  // is full-screen and OUTSIDE the shell, so it has to put the centered column back —
  // without this its panels stretch edge to edge.
  root.className = "wrap";
  const reached = deriveStep();                 // furthest step the artifacts allow
  const chips = STEPS.map((st, i) => {
    const nav = i <= reached;                    // completed steps + the current one are navigable
    const cls = [i === S.step ? "active" : (i < reached ? "done" : ""), nav ? "nav" : ""].join(" ").trim();
    return `<span class="step-chip ${cls}" ${nav ? `data-step="${i}"` : ""}><span class="n">${String(i + 1).padStart(2, "0")}</span>${st.label}</span>`;
  }).join("");
  root.innerHTML = `
    <div class="topbar">
      ${brandLockup()}
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
   Palza's own look — normal width, no borrowed chrome. */
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

// §4: an agent carries a color, a background tint, and a text tone. Elements read them as
// custom properties, so recoloring one agent restyles its avatar, dots, rings and bubbles
// together instead of each place picking its own shade of the same hue.
const AGENT_TINTS = {
  "#FF6655": ["#FFEDEA", "#D4442E"], "#FFC83D": ["#FFF6DE", "#C89011"],
  "#45B86B": ["#E8F6EC", "#2E8F50"], "#A58AE8": ["#F1EBFC", "#8465CC"],
  "#315EF6": ["#EEF2FF", "#315EF6"],
  "#FFB3A6": ["#FFEDEA", "#D4442E"], "#FF8A73": ["#FFEDEA", "#D4442E"],
  "#E04A38": ["#FFEDEA", "#B33A2B"], "#B33A2B": ["#FFEDEA", "#B33A2B"],
  "#FFE49A": ["#FFF6DE", "#A87F12"], "#FFD966": ["#FFF6DE", "#C89011"],
  "#E0A818": ["#FFF6DE", "#A87F12"], "#A87F12": ["#FFF6DE", "#A87F12"],
  "#A8E0BC": ["#E8F6EC", "#2E8F50"], "#7BD097": ["#E8F6EC", "#2E8F50"],
  "#2E8F50": ["#E8F6EC", "#1F6B3A"], "#1F6B3A": ["#E8F6EC", "#1F6B3A"],
  "#D9CCF6": ["#F1EBFC", "#8465CC"], "#C3AFF0": ["#F1EBFC", "#8465CC"],
  "#8465CC": ["#F1EBFC", "#64489F"], "#64489F": ["#F1EBFC", "#64489F"],
  "#A9BDFB": ["#EEF2FF", "#2447C4"], "#7B98F9": ["#EEF2FF", "#2447C4"],
  "#2447C4": ["#EEF2FF", "#1A3390"], "#1A3390": ["#EEF2FF", "#1A3390"],
};
function agentVars(a) {
  const c = agentColor(a);
  const [bg, fg] = AGENT_TINTS[String(c).toUpperCase()] || ["#EEF2FF", c];
  return `--ag:${c};--ag-bg:${bg};--ag-fg:${fg}`;
}

function avatar(name, size, agent) {
  const initial = (String(name || "?").trim()[0] || "?").toUpperCase();
  // An agent avatar is its own color: tinted fill, colored ring, colored initial (21).
  // Falls back to a name-derived hue when no agent is in hand (e.g. a bare name).
  const vars = agentVars(agent || { agent_id: name, color: (agent || {}).color });
  return `<span class="ag-av" style="${vars};width:${size}px;height:${size}px;
    font-size:${Math.round(size * .40)}px;background:var(--ag-bg);color:var(--ag-fg);
    border:${size >= 44 ? 3 : 2}px solid var(--ag);">${esc(initial)}</span>`;
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
  const done = p.status && p.status !== "pending";
  const why = (p.rationale || "").trim();
  // Frame 27: a titled action with the payload folded behind a disclosure, so what you
  // are approving reads in one line and the full email is one click away — rather than
  // the card dumping every field inline and burying the decision.
  const title = esc(p.summary || p.kind || "Action");
  const fields = Object.keys(pl).map(k => {
    const v = pl[k];
    const val = (v && typeof v === "object") ? JSON.stringify(v, null, 2) : String(v);
    const long = val.length > 90 || val.includes("\n");
    return `<div class="pp-field${long ? " pp-body" : ""}"><b>${esc(k)}:</b> ${esc(val)}</div>`;
  }).join("");
  return `<div class="pp-card">
    <div class="row" style="gap:9px;align-items:center;">
      <span class="pp-dot"></span>
      <span class="pp-title">${title}</span>
      ${done ? `<span class="pill ${p.status === "executed" ? "pill-on" : "pill-off"}" style="margin-left:auto;">${esc(p.status)}</span>`
             : `<button class="btn-link pp-toggle" data-id="${esc(p.id)}" style="margin-left:auto;">View full ▾</button>`}
    </div>
    ${why ? `<div class="pp-why">${esc(why)}</div>` : ""}
    <div class="pp-payload" data-for="${esc(p.id)}" hidden>${fields || `<div class="pp-field muted">No details captured.</div>`}</div>
    ${done ? "" : `<div class="row" style="gap:9px;">
      <button class="btn-accent prop-yes" data-id="${esc(p.id)}" style="flex:1;">Approve &amp; send</button>
      <button class="btn-secondary prop-edit" data-id="${esc(p.id)}">Edit first</button>
      <button class="btn-link prop-no" data-id="${esc(p.id)}" style="color:var(--ink-2);">Reject</button>
    </div>
    <div class="pp-note">Edits become lessons. Reject asks one line: why?</div>`}
  </div>`;
}

// Disclosure + per-card actions. Delegated so it survives re-renders of the list.
function wireProposalCards(root_) {
  (root_ || document).querySelectorAll(".pp-toggle").forEach(b => {
    b.onclick = () => {
      const box = (root_ || document).querySelector(`.pp-payload[data-for="${b.dataset.id}"]`);
      if (!box) return;
      box.hidden = !box.hidden;
      b.textContent = box.hidden ? "View full ▾" : "Hide full ▴";
    };
  });
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
  // The stopped-run card (16): the agent paused mid-run against one of your rules. Coral
  // border and rule label so it reads as "stopped", the question in full weight, answers
  // as chips, and the promise that answering resumes it — this is the moment the
  // human-in-the-loop loop is actually visible, so it should not look like a form field.
  const opts = (d.options || []).map(o =>
    `<button class="chip dec-opt" data-id="${esc(d.id)}" data-val="${esc(o)}">${esc(o)}</button>`).join("");
  const rule = (d.rule || "").trim();
  return `<div class="stop-card">
    <div class="stop-label">STOPPED${rule ? ` — YOUR RULE: "${esc(rule.toUpperCase())}"` : ""}</div>
    <div class="stop-q">${esc(d.question || "Needs your input")}</div>
    ${d.context ? `<div class="stop-ctx">${esc(d.context)}</div>` : ""}
    ${opts ? `<div class="chip-row">${opts}<button class="chip chip-quiet dec-explain" data-id="${esc(d.id)}">Let me explain…</button></div>` : ""}
    <div class="row dec-free" data-id="${esc(d.id)}"${opts ? ' hidden' : ''} style="gap:6px;">
      <input type="text" class="dec-answer" data-id="${esc(d.id)}" placeholder="Type your answer…" style="flex:1;">
      <button class="btn-accent btn-sm dec-submit" data-id="${esc(d.id)}">Answer</button>
    </div>
    <div class="stop-note">Answering resumes the run instantly · your answer becomes a lesson it remembers</div>
  </div>`;
}

// "Let me explain…" reveals the free-text row rather than replacing the chips, so a
// considered answer and a one-tap answer are the same interaction.
function wireDecisionCards(scope) {
  (scope || document).querySelectorAll(".dec-explain").forEach(b => b.onclick = () => {
    const row = (scope || document).querySelector(`.dec-free[data-id="${b.dataset.id}"]`);
    if (row) { row.hidden = false; const i = row.querySelector("input"); if (i) i.focus(); }
  });
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

// Home data backs EVERY main screen, and each nav click was refetching it — the most
// expensive endpoint in the app — so switching tabs waited on a full round trip every
// time. Cached briefly; any mutation sets S.igHome = null, which still forces a refetch.
const HOME_TTL_MS = 20000;
async function loadHome(force) {
  if (!force && S.igHome && (Date.now() - (S._homeAt || 0) < HOME_TTL_MS)) return S.igHome;
  const { data } = await api("GET", "/api/u/home");
  S.igHome = data || { agents: [], unread_total: 0, needs_you_total: 0 };
  S._homeAt = Date.now();
  return S.igHome;
}

// The app shell (frame 14): a persistent left sidebar plus the content column. Screens
// still write `${tb.html} …content…` into #root, and the content lands inside <main>
// because the shell's opening tags are left open — innerHTML closes them at the end of
// the fragment, which is defined behaviour, not a quirk. Keeps all six screens'
// call sites unchanged while replacing the navigation wholesale.
const NAV = [
  { key: "home", label: "Home", go: () => { S.igHome = null; renderHome(); } },
  { key: "messages", label: "Messages", go: () => renderMessages() },
  { key: "groups", label: "Groups", go: () => renderGroups() },
  { key: "sharing", label: "Sharing", go: () => renderSettings() },
  { key: "settings", label: "Settings", go: () => renderSettings() },
];

function agentsTopbar(unread, backFn, needsYou, active) {
  const ct = (n) => n > 9 ? "9+" : n;
  const initial = (S.user && S.user.email || "?").trim().charAt(0).toUpperCase();
  const items = NAV.map(n => {
    const on = n.key === (active || "home");
    const badge = (n.key === "messages" && unread)
      ? `<span class="ct-badge">${ct(unread)}</span>` : "";
    return `<button class="nav-item${on ? " active" : ""}" data-nav="${n.key}">
      <span class="nav-dot${on ? " on" : ""}"></span>${n.label}${badge}</button>`;
  }).join("");
  return {
    html: `<div class="shell"><aside class="sidebar">
        ${brandLockup()}
        ${items}
        <div class="side-foot">
          <button id="navNew" class="btn-primary full">+ New workflow</button>
          <div class="user-chip"><span class="user-av">${esc(initial)}</span>
            <span class="user-mail">${esc(S.user ? S.user.email : "")}</span>
            <button id="logout" class="btn-link" title="Sign out">Sign out</button></div>
        </div>
      </aside><main class="main">
      ${backFn ? `<button id="agBack" class="btn-link" style="margin-bottom:6px;">← back</button>` : ""}`,
    wire() {
      root.className = "";                        // the shell owns layout, not .wrap
      document.querySelectorAll(".nav-item").forEach(b => {
        b.onclick = () => {
          // Move the highlight immediately; the screen follows. Waiting for the fetch
          // before acknowledging the click is what made the sidebar feel sluggish.
          document.querySelectorAll(".nav-item").forEach(o => {
            o.classList.toggle("active", o === b);
            const d = o.querySelector(".nav-dot"); if (d) d.classList.toggle("on", o === b);
          });
          const n = NAV.find(x => x.key === b.dataset.nav);
          if (n) n.go();
        };
      });
      const nw = $("#navNew");
      if (nw) nw.onclick = recordAnother;
      $("#logout").onclick = async () => {
        await api("POST", "/api/auth/logout", {});
        S.user = null; clearTimers(); root.className = "wrap"; renderAuth();
      };
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

// Frame 25: the two dials. Segmented controls rather than dropdowns, because this is a
// consequential privacy choice and a <select> hides the alternatives behind a click.
// The three positions map onto the existing owner|team|global scopes, so the stored
// values and the API are unchanged — only how the choice is presented.
const DIAL = {
  contribute: [["owner", "Nothing"], ["team", "Lessons"], ["global", "+ patterns"]],
  consume: [["owner", "Nothing"], ["team", "Ask me first"], ["global", "Auto-adopt"]],
};
function dial(kind, current, cls) {
  return `<div class="seg" data-kind="${kind}">${DIAL[kind].map(([v, label]) =>
    `<button class="seg-opt${String(current || "") === v ? " on" : ""} ${cls}" data-val="${v}">${esc(label)}</button>`
  ).join("")}</div>`;
}

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
  const tb = agentsTopbar(home.unread_total, null, home.needs_you_total, "sharing");
  root.innerHTML = `${tb.html}
    <div class="panel">
      <h2>Lesson sharing</h2>
      <p class="lead">When your agents learn from your decisions, those anonymized lessons can help other similar agents — and theirs can help yours. Choose how much to share and use. This is your account default; each agent can override it on its own page.</p>
      <div class="dial-row">
        <div class="dial-card">
          <div class="label">You contribute</div>
          ${dial("contribute", c.contribute_scope, "set-contrib")}
          <div class="dial-note">Teammates see the rule, never the example it came from.</div>
        </div>
        <div class="dial-card">
          <div class="label">You consume</div>
          ${dial("consume", c.consume_scope, "set-consume")}
          <div class="dial-note">"Ask me first" surfaces a lesson to confirm on the agent's page.</div>
        </div>
      </div>
      <button class="btn-accent btn-sm" id="saveConsent" style="margin-top:16px;">Save</button>
      <p class="sub muted" style="margin-top:12px;">"Anonymized" means names, emails, amounts and ids are stripped and a second privacy check runs before anything leaves your account. A shared lesson only ever guides someone else's agent after they confirm it once.</p>
    </div>`;
  tb.wire();
  // A dial saves on selection — a privacy choice shouldn't sit unsaved behind a button
  // you might not press. The Save button stays for the per-agent overrides below.
  const picked = { contribute: c.contribute_scope, consume: c.consume_scope };
  root.querySelectorAll(".seg-opt").forEach(b => b.onclick = async () => {
    const kind = b.closest(".seg").dataset.kind;
    picked[kind] = b.dataset.val;
    b.closest(".seg").querySelectorAll(".seg-opt").forEach(o => o.classList.toggle("on", o === b));
    await api("POST", "/api/u/consent", {
      contribute_scope: picked.contribute, consume_scope: picked.consume });
  });

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
  const tb = agentsTopbar(home.unread_total, null, home.needs_you_total, "groups");
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
    const tb = agentsTopbar(0, renderGroups, 0, "groups");
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
        msgs.push({ who: "sys", name: "palza", color: "#FF6655",
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

// An agent's color: its stored choice, else a deterministic pick from the core five so
// the same agent is always the same color (mirrors registry.agent_color server-side).
const CORE_AGENT_COLORS = ["#FF6655", "#FFC83D", "#45B86B", "#A58AE8", "#315EF6"];
function agentColor(a) {
  if (a && a.color) return a.color;
  const id = String((a && (a.agent_id || a.id)) || "");
  let sum = 0;
  for (let i = 0; i < id.length; i++) sum += id.charCodeAt(i);
  return CORE_AGENT_COLORS[sum % CORE_AGENT_COLORS.length];
}

// The full agent list, reached from the brief's "See all runs" — the detail behind the
// headline, rather than the landing screen it used to be.
async function renderAllRuns() {
  const home = await loadHome();
  const agents = home.agents || [];
  const tb = agentsTopbar(home.unread_total, null, home.needs_you_total, "home");
  const rows = agents.map(a => {
    const sched = a.schedule || null;
    const bits = [];
    if (a.open_decisions) bits.push(`${a.open_decisions} awaiting your answer`);
    if (a.pending_approvals) bits.push(`${a.pending_approvals} to approve`);
    if (a.fyi_reviews) bits.push(`${a.fyi_reviews} to review`);
    if (a.unread) bits.push(`${a.unread} new message${a.unread > 1 ? "s" : ""}`);
    bits.push(sched ? (sched.paused ? "schedule paused" : `runs ${cadenceToValue(sched.cadence) || "on its schedule"}`)
                    : "runs when you say");
    return `<button class="ag-row" data-agent="${esc(a.agent_id)}">
      ${avatar(a.name, 44, a)}
      <span class="ag-mid">
        <span class="title">${esc(a.name)}</span>
        <span class="ag-bio">${esc(bits.join(" · "))}</span>
      </span>
      <span class="pill ${sched && sched.paused ? "pill-off" : "pill-on"}">${sched && sched.paused ? "PAUSED" : "LIVE"}</span>
    </button>`;
  }).join("");
  root.innerHTML = `${tb.html}
    <div class="panel">
      <div class="spread"><h2>Your agents</h2>
        <button id="recAnother" class="btn-secondary btn-sm">Record another workflow</button></div>
      <p class="lead">Every workflow you've automated. Open one to see its runs, rules and lessons.</p>
      <div class="stack" style="margin-top:14px;">${rows || `<p class="muted">No agents yet.</p>`}</div>
    </div>`;
  tb.wire();
  const ra = $("#recAnother"); if (ra) ra.onclick = recordAnother;
  root.querySelectorAll(".ag-row").forEach(b => b.onclick = () => renderProfile(b.dataset.agent));
}

async function renderHome() {
  const home = await loadHome();
  const agents = home.agents || [];
  const tb = agentsTopbar(home.unread_total, null, home.needs_you_total, "home");
  const need = agents.filter(a => a.needs_you > 0)
    .sort((x, y) => (y.open_decisions - x.open_decisions) || (y.needs_you - x.needs_you));
  const clean = agents.filter(a => !a.needs_you);
  const total = home.needs_you_total || 0;

  // The morning brief (14): headline states the situation, the sentence below names the
  // agents involved in their own colors, and the Overnight strip is the per-agent detail.
  const now = new Date();
  const stamp = now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })
    + " · " + now.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const headline = total
    ? `${total === 1 ? "One thing needs" : total + " things need"} you.<br>Everything else ran clean.`
    : "Everything ran clean.<br>Nothing needs you.";
  const tint = (a) => `<span style="color:${esc(agentColor(a))};font-weight:600;">${esc(a.name)}</span>`;
  const phrase = (a) => {
    if (a.open_decisions) return `${tint(a)} hit ${a.open_decisions === 1 ? "a question" : a.open_decisions + " questions"} it can't settle`;
    if (a.pending_approvals) return `${tint(a)} staged ${a.pending_approvals} ${a.pending_approvals === 1 ? "action" : "actions"} for your sign-off`;
    return `${tint(a)} has a lesson to confirm`;
  };
  const lead = need.length
    ? need.slice(0, 2).map(phrase).join(", and ") + "."
      + (clean.length ? ` ${clean.slice(0, 3).map(a => esc(a.name)).join(", ")} finished with nothing to show you.` : "")
    : "Your agents ran and handled everything themselves.";

  const card = (a) => {
    const bits = [];
    if (a.open_decisions) bits.push(`${a.open_decisions} question${a.open_decisions > 1 ? "s" : ""}`);
    if (a.pending_approvals) bits.push(`${a.pending_approvals} staged`);
    if (!bits.length) bits.push(a.fyi_reviews ? `${a.fyi_reviews} run${a.fyi_reviews > 1 ? "s" : ""}, clean` : "nothing new");
    const last = (a.stories || []).slice(-1)[0];
    return `<button class="ov-card" data-agent="${esc(a.agent_id)}"${a.needs_you ? "" : ' style="opacity:.6;"'}>
      <span class="row" style="gap:8px;">
        <span class="ov-dot" style="background:${esc(agentColor(a))};"></span>
        <span class="ov-name">${esc(a.name)}</span>
        ${a.needs_you ? `<span class="ov-flag"></span>` : ""}
      </span>
      <span class="ov-sub">${esc(bits.join(" · "))}${last && last.ran_at ? " · " + esc(ago(last.ran_at)) : ""}</span>
    </button>`;
  };

  root.innerHTML = `${tb.html}
    <div class="brief">
      <div class="brief-stamp">${esc(stamp)}</div>
      <h1 class="brief-head">${headline}</h1>
      <p class="brief-lead">${lead}</p>
      <div class="row" style="gap:10px;">
        ${total ? `<button id="hmReview" class="btn-accent" style="padding:13px 26px;">Start review · ${total}</button>` : ""}
        <button id="hmAll" class="btn-secondary" style="padding:13px 22px;">See all runs</button>
      </div>
      ${agents.length ? `<div class="ov-wrap">
        <div class="label">Overnight</div>
        <div class="ov-row">${need.concat(clean).map(card).join("")}</div>
      </div>` : `<p class="muted">No agents yet — record a workflow to build your first.</p>`}
    </div>`;
  tb.wire();
  const first = need[0] || agents[0];
  const rv = $("#hmReview");
  if (rv && first) rv.onclick = () => renderReview(first.agent_id, 0);
  $("#hmAll").onclick = renderAllRuns;
  root.querySelectorAll(".ov-card").forEach(b =>
    b.onclick = () => renderProfile(b.dataset.agent));
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
    wireProposalCards(root);                      // frame 27 payload disclosure
    // Each card approves itself now — the checkbox-and-bulk-button flow made you decide
    // about several actions at once, which is the opposite of reading one and approving it.
    root.querySelectorAll(".prop-yes").forEach(b => b.onclick = async () => {
      b.disabled = true; b.textContent = "Sending…";
      await api("POST", `/api/u/agents/${agentId}/approve-actions`, { pending_ids: [b.dataset.id] });
      await markReviewed(st);
      S._timers.push(setTimeout(async () => {
        await loadHome();
        const fresh = (S.igHome.agents || []).find(x => x.agent_id === agentId);
        if (fresh && fresh.stories.length) renderReview(agentId, Math.min(i, fresh.stories.length - 1));
        else close();
      }, 3500));
    });
    root.querySelectorAll(".prop-edit").forEach(b => b.onclick = () => {
      // Editing opens the payload; saving an edit as a lesson is step 6's job.
      const box = root.querySelector(`.pp-payload[data-for="${b.dataset.id}"]`);
      if (box) box.hidden = false;
    });
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
    wireDecisionCards(root);
    wireDecisionCards(root);
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
  const tb = agentsTopbar(d.unread_total, null, d.needs_you_total, "messages");
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
      ${avatar(a.name, 44, a)}
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
  const home = await loadHome();
  const me = (home.agents || []).find(x => x.agent_id === agentId) || { name: "Agent" };
  const tb = agentsTopbar(home.unread_total, renderMessages, home.needs_you_total, "messages");
  // Frame 23: the thread says what it can actually do — answers come from its run
  // history, not from a fresh session — and offers openers, so the box isn't a blank
  // prompt you have to invent a question for.
  const SUGGESTIONS = ["What did you do today?", "Anything you're stuck on?",
                       "What did you skip and why?"];
  root.innerHTML = `${tb.html}
    <div class="panel dm-panel">
      <div class="dm-head">
        ${avatar(me.name, 44, me)}
        <div style="flex:1;">
          <div class="title">${esc(me.name)}</div>
          <div class="dm-live"><span class="dm-live-dot"></span>live · answers from its run history</div>
        </div>
        <button id="thProfile" class="btn-secondary btn-sm">View profile</button>
      </div>
      <div class="dm-chat" id="chat"><p class="muted">Loading…</p></div>
      <div class="chip-row" id="dmSuggest">${SUGGESTIONS.map(t =>
        `<button class="chip chip-quiet dm-sug">${esc(t)}</button>`).join("")}</div>
      <div class="dm-bar">
        <input id="dmText" type="text" placeholder="Message…" autocomplete="off">
        <button id="dmSend" class="dm-send" title="Send">↑</button>
      </div>
    </div>`;
  tb.wire();
  const pf = $("#thProfile");
  if (pf) pf.onclick = () => renderProfile(agentId);
  root.querySelectorAll(".dm-sug").forEach(b => b.onclick = () => {
    const i = $("#dmText"); if (i) { i.value = b.textContent; i.focus(); }
  });
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

/* ---------------- identity popover (19) ---------------- */
// Five core shades, then 20 extended (4 per family) behind "More shades" — same five
// families, so a crowded team never drifts into a rainbow. A shade already in use shows
// its owner's initial, making a twin a choice rather than an accident (§4).
const EXTENDED_SHADES = [
  "#FFB3A6", "#FF8A73", "#E04A38", "#B33A2B",
  "#FFE49A", "#FFD966", "#E0A818", "#A87F12",
  "#A8E0BC", "#7BD097", "#2E8F50", "#1F6B3A",
  "#D9CCF6", "#C3AFF0", "#8465CC", "#64489F",
  "#A9BDFB", "#7B98F9", "#2447C4", "#1A3390",
];

function openIdentityPopover(agent, agentId) {
  const taken = {};                                  // color -> another agent's initial
  ((S.igHome && S.igHome.agents) || []).forEach(x => {
    if (x.agent_id !== agentId && x.color) taken[String(x.color).toUpperCase()] = (x.name || "?").charAt(0).toUpperCase();
  });
  const chosen = { color: agentColor(agent) };
  const swatch = (c) => {
    const who = taken[c.toUpperCase()];
    const on = c.toUpperCase() === String(chosen.color).toUpperCase();
    return `<button class="sw${on ? " on" : ""}" data-color="${esc(c)}"
      style="background:${esc(c)};${who ? "opacity:.35;" : ""}">${
        who ? `<span class="sw-who">${esc(who)}</span>` : ""}</button>`;
  };
  const el = document.createElement("div");
  el.className = "pop-back";
  el.innerHTML = `<div class="pop">
      <div class="row" style="gap:12px;">
        ${avatar(agent.name, 52, agent)}
        <div class="pop-title">Edit agent</div>
        <button class="pop-x" title="Close">✕</button>
      </div>
      <div class="field"><span class="label">Name</span>
        <input id="popName" type="text" maxlength="80" value="${esc(agent.name || "")}"></div>
      <div class="field"><span class="label">Color</span>
        <div class="sw-row">${CORE_AGENT_COLORS.map(swatch).join("")}</div>
        <button class="btn-link" id="popMore" style="padding-left:0;">More shades ▾</button>
        <div class="sw-row" id="popExt" hidden>${EXTENDED_SHADES.map(swatch).join("")}</div>
      </div>
      <div class="row" style="gap:9px;">
        <button class="btn-accent" id="popSave" style="flex:1;">Save</button>
        <button class="btn-secondary" id="popCancel">Cancel</button>
      </div>
      <p class="pop-note">Changing the color recolors this agent everywhere — avatar, chat
        bubbles, badges. A shade showing an initial is already another agent's.</p>
    </div>`;
  document.body.appendChild(el);
  const close = () => el.remove();
  el.onclick = (e) => { if (e.target === el) close(); };
  el.querySelector(".pop-x").onclick = close;
  el.querySelector("#popCancel").onclick = close;
  el.querySelector("#popMore").onclick = () => {
    const ext = el.querySelector("#popExt");
    ext.hidden = !ext.hidden;
    el.querySelector("#popMore").textContent = ext.hidden ? "More shades ▾" : "Fewer shades ▴";
  };
  el.querySelectorAll(".sw").forEach(b => b.onclick = async () => {
    chosen.color = b.dataset.color;
    el.querySelectorAll(".sw").forEach(o => o.classList.toggle("on", o === b));
    // Apply on click: picking a color IS the decision, and §4 says a recolor applies
    // live everywhere. Save then only has the name left to commit.
    try {
      await api("POST", `/api/u/agents/${agentId}/color`, { color: chosen.color });
      agent.color = chosen.color;
      S.igHome = null;
    } catch (e) { /* the Save button retries */ }
  });
  el.querySelector("#popSave").onclick = async () => {
    const save = el.querySelector("#popSave");
    save.disabled = true; save.textContent = "Saving…";
    const name = (el.querySelector("#popName").value || "").trim();
    try {
      if (name && name !== agent.name) await api("POST", `/api/u/agents/${agentId}/name`, { name });
      // Always send the color: agent.color is often absent (the UI was showing a
      // derived default), so "unchanged" compared against nothing and skipped the save.
      if (chosen.color) await api("POST", `/api/u/agents/${agentId}/color`, { color: chosen.color });
      S.igHome = null;
      close();
      renderProfile(agentId);                        // recolor applies everywhere at once
    } catch (e) { save.disabled = false; save.textContent = "Couldn't save"; }
  };
}

async function renderProfile(agentId) {
  clearTimers();
  const home = S.igHome || await loadHome();
  const a = (home.agents || []).find(x => x.agent_id === agentId) || {};
  const name = a.name || agentId;
  const sched = a.schedule || null;
  const cur = cadenceToValue(sched && sched.cadence);
  const paused = sched && sched.paused;
  const opts = CADENCES.map(c => `<option value="${c.v}" ${c.v === cur ? "selected" : ""}>${c.label}</option>`).join("");
  const tb = agentsTopbar(home.unread_total, null, home.needs_you_total, "home");
  root.innerHTML = `${tb.html}
    <div class="panel">
      <div class="row" style="gap:16px;align-items:flex-start;">
        ${avatar(name, 64)}
        <div style="flex:1;">
          <div class="row" style="gap:10px;"><h2 id="pTitle" style="margin:0;">${esc(name)}</h2>
            <button id="pEdit" class="pencil" title="Rename or recolor">✎</button>
            <span class="pill ${paused ? "pill-warn" : "pill-on"}">${paused ? "PAUSED" : "LIVE"}</span></div>
          <p class="ag-bio wrap" style="margin:6px 0 0;">${esc(a.description || ("Runs when: " + (a.trigger || "you say so")))}</p>
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
      <div class="tabs" id="pTabs">
        ${["Overview", "Rules", "Lessons", "History"].map((t, n) =>
          `<button class="tab${n === 0 ? " on" : ""}" data-pane="${t.toLowerCase()}">${t}</button>`).join("")}
      </div>
      <div class="row" style="margin-top:12px;">
        ${a.capture_present
          ? `<button id="pDelCap" class="btn-link" style="color:var(--danger,#b3261e);">Delete capture data</button>`
          : `<span class="muted" style="font-size:12px;">Capture data deleted</span>`}
      </div>
    </div>
    ${(a.lesson_confirmations && a.lesson_confirmations.length) ? `
    <div class="panel" data-pane="lessons">
      <h2>Shared lessons to confirm</h2>
      <p class="sub muted" style="margin-top:2px;">Lessons a similar agent learned. Confirm one and this agent applies it on its own from then on; dismiss and it won't come back.</p>
      <div style="margin-top:10px;">${a.lesson_confirmations.map(c => lessonConfirmCard(agentId, c)).join("")}</div>
    </div>` : ""}
    ${(a.active_lessons && a.active_lessons.length) ? `
    <div class="panel" data-pane="lessons">
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
  // Identity lives in one popover now (frame 19), reached from the pencil in the header —
  // §4 retires the inline name/color rows. Applying a change updates the page in place:
  // re-rendering the profile would refetch the activity log, which is far too slow for
  // picking a color.
  const pe = $("#pEdit");
  if (pe) pe.onclick = () => openIdentityPopover(a, agentId);

  // Tabs (21). Panels are assigned to a pane; anything untagged belongs to Overview, so
  // a panel added later shows up rather than vanishing.
  const panes = () => Array.from(root.querySelectorAll(".panel[data-pane], .panel"));
  const showPane = (want) => {
    root.querySelectorAll(".panel").forEach((el, idx) => {
      if (idx === 0) return;                        // the header panel is always visible
      const pane = el.dataset.pane || "overview";
      el.style.display = (pane === want) ? "" : "none";
    });
    root.querySelectorAll("#pTabs .tab").forEach(t =>
      t.classList.toggle("on", t.dataset.pane === want));
  };
  root.querySelectorAll("#pTabs .tab").forEach(t =>
    t.onclick = () => showPane(t.dataset.pane));
  showPane("overview");

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
  wireDecisionCards(root);
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
      <p class="lead">Palza watches how you work for a while and learns your routines. On your Mac · no screenshots · sensitive text removed before saving.</p>
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
      <p class="notice">The more concretely you name your apps and steps, the better your agent will be. You'll review and correct what palza understood next.</p>
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
      <p class="lead">Palza reads back your day and groups the repeating work it can take off your plate.</p>
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
  // plain-language summary of what palza thinks this workflow is (anti_bundles
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
  // ONE WORKFLOW PER PAGE. Every workflow's questions used to render on a single page —
  // three workflows meant ~30 questions in one endless scroll, with no sense of progress
  // and no way to tell whose questions you were answering.
  const allIds = data.approved_ids || Object.keys(interviews);
  const ids = allIds.filter(bid => ((interviews[bid] || {}).questions || []).length);
  const nameOf = (bid) => (data.names && data.names[bid]) || bid;
  let page = 0;
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
    // Optional free-text: instructions the generated questions never thought to ask.
    // Never gates the step — it is not a question and never counts as unresolved.
    const extra = prior["extra_notes"] || "";
    return `<div class="iv-page" data-bid="${esc(bid)}" style="display:none;">
      <div class="card" style="padding:16px;">
        <div class="label" style="margin-bottom:10px;">${esc(nameOf(bid))}</div>${qs}
        <div class="field" style="margin-top:14px;">
          <div class="title" style="margin-bottom:6px;">Anything else we should know? <span class="muted" style="font-weight:400;">(optional)</span></div>
          <textarea class="iv-extra" data-bid="${esc(bid)}" rows="3" style="min-height:72px;"
            placeholder="Anything the questions above didn't cover — exceptions, preferences, things to avoid">${esc(extra)}</textarea>
        </div>
      </div></div>`;
  }).join("");
  $("#ivBody").innerHTML = `
    ${blocks || `<p class="muted">No questions needed — you're good to go.</p>`}
    <p id="ivProg" class="sub muted" style="margin-top:10px;"></p>
    <p id="ivMsg" class="msg ok" style="margin-top:6px;"></p>
    <div class="row" style="margin-top:6px;gap:10px;">
      <button id="ivBack" class="btn-secondary" style="display:none;">Back</button>
      <button id="ivGo" class="btn-primary" style="flex:1;">Save &amp; continue</button>
    </div>`;
  // Only the current workflow's answers gate the button, so an unanswered question three
  // workflows away can't silently block you with no clue why.
  const pageInputs = () => {
    const el = root.querySelectorAll(".iv-page")[page];
    return el ? Array.from(el.querySelectorAll(".iv-a")) : [];
  };
  const showPage = () => {
    root.querySelectorAll(".iv-page").forEach((el, i) => {
      el.style.display = (i === page) ? "" : "none";
    });
    const last = page >= ids.length - 1;
    const prog = $("#ivProg");
    if (prog) {
      prog.textContent = ids.length > 1
        ? `Workflow ${page + 1} of ${ids.length} — ${nameOf(ids[page])}` : "";
    }
    const back = $("#ivBack");
    if (back) back.style.display = (page > 0) ? "" : "none";
    const go = $("#ivGo");
    if (go) go.textContent = last ? "Save & continue" : "Next workflow";
    updateGo();
    if (typeof window.scrollTo === "function") window.scrollTo(0, 0);
  };
  const updateGo = () => {
    $("#ivGo").disabled = pageInputs().some(inp => !inp.value.trim());
  };
  root.querySelectorAll(".iv-a").forEach(inp => inp.oninput = updateGo);
  root.querySelectorAll(".suggest").forEach(btn => btn.onclick = () => {
    const inp = root.querySelector(`.iv-a[data-bid="${btn.dataset.bid}"][data-q="${btn.dataset.q}"]`);
    if (inp) { inp.value = btn.textContent; updateGo(); }
  });
  const back = $("#ivBack");
  if (back) back.onclick = () => { if (page > 0) { page -= 1; showPage(); } };
  showPage();
  const saveAll = async () => {
    const byB = {};
    root.querySelectorAll(".iv-a").forEach(inp => {
      const v = inp.value.trim(); if (!v) return;
      (byB[inp.dataset.bid] = byB[inp.dataset.bid] || {})[inp.dataset.q] = v;
    });
    root.querySelectorAll(".iv-extra").forEach(t => {
      const v = (t.value || "").trim(); if (!v) return;
      (byB[t.dataset.bid] = byB[t.dataset.bid] || {}).extra_notes = v;
    });
    for (const bid of Object.keys(byB)) {
      const r = await upost(sid, `/interview/${bid}`, { answers: byB[bid] });
      if (!r.ok || (r.data && r.data.status && r.data.status !== "complete")) {
        throw new Error((r.data && r.data.error) || "answers didn't save — try again");
      }
    }
  };
  $("#ivGo").onclick = async () => {
    if (page < ids.length - 1) { page += 1; showPage(); return; }   // next workflow
    $("#ivGo").disabled = true; $("#ivGo").textContent = "Saving…";
    try {
      await saveAll();                        // every page's answers, in one pass
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
    <p class="lead">Sign in with each service. Palza never sees your password, and only uses accounts that match your work email.</p>
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
  let url = null, reused = false, reusedEmail = "";
  try {
    const d = await pollJob(sid, "connect", () => {});
    url = d.connect_url; reused = !!d.reused; reusedEmail = d.identity_email || "";
  } catch (e) { $("#connMsg").textContent = friendly(e.message); return; }
  if (reused) {
    // This person already approved the app for another agent — approvals are per
    // account, so they carry over. No browser round trip needed; just verify.
    $("#connMsg").innerHTML =
      `<b>${esc(slug)}</b> is already connected${reusedEmail ? ` as ${esc(reusedEmail)}` : ""}. ` +
      `Click verify to use it for this agent.`;
  } else if (url) {
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
