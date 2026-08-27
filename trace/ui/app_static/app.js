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
// No request may hang a promise forever: the desktop node's cloud proxy times out
// at 60s, so abort just above that — the server's error gets first claim, and a
// dead network can't leave the splash (or any spinner) up indefinitely.
const API_TIMEOUT_MS = 65000;
async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" }, credentials: "same-origin" };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), API_TIMEOUT_MS);
  opt.signal = ctl.signal;
  try {
    const r = await fetch(path, opt);
    let data = null;
    try { data = await r.json(); } catch (e) { /* empty */ }
    return { ok: r.ok, status: r.status, data: data || {} };
  } finally {
    clearTimeout(timer);
  }
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
// Frame 07's mark: the same five routes as logoMark, drawing themselves in on a stagger
// while discovery runs. The routes converging on the hub is the thing being described —
// scattered work becoming one workflow — so it says more than a spinner would.
const MARK_ROUTES = [
  ["M6 9 C52 9 52 45 82 45", "#FF6655"], ["M6 27 C48 27 48 45 82 45", "#FFC83D"],
  ["M6 45 H82", "#315EF6"], ["M6 63 C48 63 48 45 82 45", "#45B86B"],
  ["M6 81 C52 81 52 45 82 45", "#A58AE8"],
];
function searchMark() {
  return `<svg width="110" height="90" viewBox="0 0 110 90" fill="none" aria-hidden="true">
    ${MARK_ROUTES.map(([d, c], i) => `<path d="${d}" stroke="${c}" stroke-width="8"
      stroke-linecap="round" stroke-dasharray="170"
      style="animation:palzaDraw 3s ease-in-out ${(i * 0.15).toFixed(2)}s infinite"/>`).join("")}
    <circle cx="90" cy="45" r="13" fill="#121A2F"/><circle cx="90" cy="45" r="5" fill="#FFFFFF"/>
  </svg>`;
}

// "6h 12m captured · " when the session's own timestamps allow it, else "". Derived here
// rather than added to the API: the sessions payload already carries created_at and
// stopped_at, so the duration is a subtraction, not a new field.
function capturedFor(session) {
  const a = session && session.created_at, b = session && session.stopped_at;
  const ms = (a && b) ? Date.parse(b) - Date.parse(a) : NaN;
  if (!ms || ms < 0) return "";
  const mins = Math.round(ms / 60000);
  const h = Math.floor(mins / 60), m = mins % 60;
  return (h ? `${h}h ${m}m` : `${m}m`) + " captured · ";
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

// The splash must come down no matter how boot ends: hideSplash() lives in the
// finally, and a failure lands on a visible retry card instead of a dead splash.
// A slow start (the cloud service waking up) gets a status line after 8s so it
// reads as progress, not a hang.
async function boot() {
  const still = setTimeout(() => {
    const sp = $("#splash");
    if (!sp || $("#splashStatus")) return;
    const note = document.createElement("div");
    note.id = "splashStatus";
    note.style.cssText = "margin-top:16px;color:#888;font-size:14px;";
    note.textContent = "Still connecting — the service may be waking up…";
    sp.appendChild(note);
  }, 8000);
  try {
    const { data } = await api("GET", "/api/auth/me");
    S.signupAllowed = data.signup_enabled !== false;
    S.user = data.user;
    if (!S.user) return renderAuth();
    await routeSignedIn();
  } catch (e) {
    renderBootError(e);
  } finally {
    clearTimeout(still);
    hideSplash();
  }
}

function renderBootError(err) {
  const msg = String(err && err.name === "AbortError"
    ? "the service is taking too long to respond" : friendly(err && err.message));
  root.innerHTML = `
    <div class="card" style="max-width:420px;margin:120px auto;text-align:center;">
      <div class="title">Couldn't reach palza</div>
      <p class="muted" style="margin-top:8px;">We couldn't load the app — ${esc(msg)}.
        Check your connection and try again.</p>
      <button id="bootRetry" class="btn-primary" style="margin-top:14px;">Try again</button>
    </div>`;
  $("#bootRetry").onclick = () => { root.innerHTML = ""; boot(); };
}

// Where a signed-in account belongs: a brand-new one is walked through Welcome (04) →
// Privacy & terms (05), one that has finished onboarding lands on Home, one caught
// mid-setup resumes the wizard.
//
// EVERY path that authenticates must come through here. Signing up used to call
// loadAndRender() directly, which dropped straight into the wizard at Record — so a new
// account never saw Welcome or the terms gate, and the only way to reach either was to
// reload the page afterwards. The screens were built; nothing routed to them.
async function routeSignedIn() {
  const fr = await api("GET", "/api/u/first-run");
  if (!(fr.data && fr.data.terms_accepted_at)) return renderWelcome();
  if (fr.data.first_run_completed_at) { S.igHome = null; return renderHome(); }
  return loadAndRender();
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
      <button id="frGo" class="btn-primary" style="padding:13px 34px;margin-top:6px;">Get started</button>
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
        <span style="font-size:12.5px;color:var(--ink-2);">I agree to the
          <button type="button" class="fr-link" id="frTerms">terms of service</button> and
          <button type="button" class="fr-link" id="frPriv">privacy policy</button></span>
      </label>
      <button id="frAccept" class="btn-primary full" disabled>Agree &amp; continue</button>
    </div>`;
  $("#frAgree").onchange = (e) => { $("#frAccept").disabled = !e.target.checked; };
  // Frame 05 makes these links. They were flat grey text, which meant asking someone to
  // agree to documents they could not open — on the one screen where that matters, since
  // the thing being agreed to is recording their work.
  //
  // No formal documents exist in this repo yet. Rather than a dead link or invented legal
  // prose, each opens what the product ACTUALLY does — enforced in code, and checkable —
  // and says plainly that the formal document is still to be published.
  $("#frTerms").onclick = (e) => { e.preventDefault(); showPolicy("terms"); };
  $("#frPriv").onclick = (e) => { e.preventDefault(); showPolicy("privacy"); };
  $("#frAccept").onclick = async () => {
    $("#frAccept").disabled = true;
    await api("POST", "/api/u/first-run/terms", {});
    await api("POST", "/api/u/first-run/complete", {});
    await loadAndRender();                       // straight into the wizard at Record
  };
}

// What the product commits to, stated as behaviour rather than as law. Every line here
// is enforced somewhere in the code and can be checked; none of it is drafted legal
// prose, because inventing that would be worse than admitting it isn't written yet.
const POLICY = {
  terms: ["Terms of service", [
    ["What palza does", "Records a workday you start, finds the work that repeats, and builds an agent for each. You decide which workflows become agents and you can delete any of them."],
    ["Nothing outbound without you", "Every email, sheet edit and upload an agent proposes is staged and shown to you in full before it happens. That gate cannot be turned off for outbound actions."],
    ["Your agents are yours", "You can pause, rename, recolour or delete any agent at any time, and revoke an app's access from Settings."],
  ]],
  privacy: ["Privacy policy", [
    ["Recording is opt-in and visible", "Capture only runs during a session you start, with a live indicator, and you can pause or stop it at any moment."],
    ["What is captured", "Window titles, the text you type, and the labels and values of the controls you use — the buttons, fields and dropdowns you actually touch. Still no screenshots and no images of your screen: palza reads these as text, the same way a screen reader does. Password fields are never read, and sensitive text is removed before anything is saved."],
    ["What it is used for", "Building and improving your own agents. Delete every recording at any time from Settings → Privacy & data."],
    ["Sharing is yours to set", "Lessons your agents learn can be shared with your team or turned off entirely, in Sharing. Lessons carry the rule, never the email, data or recording it came from."],
  ]],
};

function showPolicy(kind) {
  const [title, sections] = POLICY[kind] || POLICY.terms;
  const el = document.createElement("div");
  el.className = "pop-back";
  el.innerHTML = `<div class="pop" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="row"><div class="pop-title">${esc(title)}</div>
        <button class="pop-x" aria-label="Close">✕</button></div>
      <div class="pol-body">${sections.map(([h, b]) =>
        `<div class="pol-sec"><div class="pol-h">${esc(h)}</div>
          <div class="pol-p">${esc(b)}</div></div>`).join("")}</div>
      <p class="pop-note">This describes how palza behaves today, and each point is
        enforced in the product. The full legal document is not published yet — ask us
        before relying on it for anything contractual.</p>
      <div class="cf-acts"><button class="btn-primary" id="polOk">Close</button></div>
    </div>`;
  document.body.appendChild(el);
  const close = () => el.remove();
  el.onclick = (e) => { if (e.target === el) close(); };
  el.querySelector(".pop-x").onclick = close;
  el.querySelector("#polOk").onclick = close;
  el.querySelector("#polOk").focus();
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
      <div class="field"><span class="label">Password</span>
        <div class="pw-wrap">
          <input id="pw" type="password" autocomplete="current-password" placeholder="${isSignup ? "at least 8 characters" : "••••••••"}">
          <button type="button" id="pwShow" class="pw-show" aria-pressed="false">show</button>
        </div>
      </div>
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
    await routeSignedIn();
  };
  $("#authGo").onclick = go;
  $("#pw").onkeydown = (e) => { if (e.key === "Enter") go(); };
  // Frame 20's reveal. A password you cannot check is a password you mistype, and the
  // remedy is on screen rather than a second confirm field.
  $("#pwShow").onclick = () => {
    const inp = $("#pw"), on = inp.type === "password";
    inp.type = on ? "text" : "password";
    $("#pwShow").textContent = on ? "hide" : "show";
    $("#pwShow").setAttribute("aria-pressed", on ? "true" : "false");
    inp.focus();
  };
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
    const done = i !== S.step && i < reached;
    const cls = [i === S.step ? "active" : (done ? "done" : ""), nav ? "nav" : ""].join(" ").trim();
    // "✓ Record" once a step is behind you, "2 Find work" otherwise (07–13). The number
    // is only useful while the step is still ahead — after that, what you want to know
    // is that it's finished.
    return `<span class="step-chip ${cls}" ${nav ? `data-step="${i}"` : ""}><span class="n">${done ? "✓" : i + 1}</span>${st.label}</span>`;
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

// Frame 22 stamps a thread with a clock time — "9:12", "Mon" — not "2h ago". In a list
// you scan for the message you remember, and you remember it as this morning or Monday.
// ago() stays for everywhere that describes a single event's recency.
// "May 12" (21). "live since 3d ago" reads as a stumble — since is a point, not a span.
function onDate(iso) {
  const t = Date.parse(iso || "");
  if (isNaN(t)) return "";
  const d = new Date(t), opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

// "ran today, 7:00 AM" (15). The story overlay was printing the raw ISO string.
function ranWhen(iso) {
  const t = Date.parse(iso || "");
  if (isNaN(t)) return "";
  const d = new Date(t), now = new Date();
  const clock = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `ran today, ${clock}`;
  if ((now - d) / 86400000 < 7)
    return `ran ${d.toLocaleDateString(undefined, { weekday: "long" })}, ${clock}`;
  return `ran ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${clock}`;
}

// (named threadStamp, not stamp: drawHome declares a local `const stamp` for its date
// line, which would shadow this inside that function.)
function threadStamp(iso) {
  const t = Date.parse(iso || "");
  if (isNaN(t)) return "";
  const d = new Date(t), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if ((now - d) / 86400000 < 7) return d.toLocaleDateString(undefined, { weekday: "short" });
  return d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
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
  // The OUTCOME LEADS. Once an action has been carried out, what happened is the first
  // thing on the card — above the title and the reasoning, which are now history. It
  // used to be a small pill floating at the right-hand end of the title row, read after
  // the description of something already done.
  // Same words the optimistic click-time card uses (settleProposalCard), so a reload
  // lands on the state the user already saw rather than a different phrasing of it.
  const outcome = {
    executed: { cls: "ok", text: "✓ Done — this was sent" },
    queued: { cls: "ok", text: "Queued…" },
    executing: { cls: "ok", text: "Sending…" },
    rejected: { cls: "bad", text: "Rejected — this won't be sent" },
  }[p.status] || { cls: "bad", text: `Not sent — ${esc(p.status)}` };
  return `<div class="pp-card${done ? " pp-card-done" : ""}" data-card="${esc(p.id)}">
    ${done ? `<div class="pp-outcome ${outcome.cls}">${outcome.text}</div>` : ""}
    <div class="row" style="gap:9px;align-items:center;">
      <span class="pp-dot"></span>
      <span class="pp-title">${title}</span>
      ${done ? "" : `<button class="btn-link pp-toggle" data-id="${esc(p.id)}" style="margin-left:auto;">Hide full ▴</button>`}
    </div>
    ${why ? `<div class="pp-why">${esc(why)}</div>` : ""}
    <!-- OPEN BY DEFAULT. You are approving the payload, not the summary — a one-line
         "send confirmation email" can sit above a body promising a delivery date, and
         hiding the text behind a click means the thing actually being approved is the
         thing you did not read. Frame 27's canonical state is open too ("payload open
         + edit"); collapsing it was a scannability choice that cost more than it paid. -->
    <div class="pp-payload" data-for="${esc(p.id)}">${fields || `<div class="pp-field muted">No details captured.</div>`}</div>
    ${done ? "" : `<div class="pp-acts" data-acts="${esc(p.id)}">
      <div class="row" style="gap:9px;">
        <button class="btn-primary prop-yes" data-id="${esc(p.id)}" style="flex:1;">Approve &amp; send</button>
        <button class="btn-secondary prop-edit" data-id="${esc(p.id)}">Edit first</button>
        <button class="btn-link prop-no" data-id="${esc(p.id)}" style="color:var(--ink-2);">Reject</button>
      </div>
      <div class="pp-note">Edits become lessons. Reject asks one line: why?</div>
    </div>`}
  </div>`;
}

// Replace a card's buttons with what actually happened, the moment it happens. The
// server takes seconds to carry an action out and the refresh is slower still; leaving
// live Approve & send buttons under a thing you already approved invites a second click
// on the same email.
function settleProposalCard(id, text, ok) {
  const card = document.querySelector(`[data-card="${CSS.escape(String(id))}"]`);
  if (!card) return;
  const acts = card.querySelector(`[data-acts="${CSS.escape(String(id))}"]`);
  if (acts) acts.remove();
  card.classList.add("pp-card-done");
  const toggle = card.querySelector(".pp-toggle");
  if (toggle) toggle.remove();
  if (!card.querySelector(".pp-outcome")) {
    card.insertAdjacentHTML("afterbegin",
      `<div class="pp-outcome ${ok ? "ok" : "bad"}">${esc(text)}</div>`);
  }
}

// Approve / reject wiring for the profile's Session history. prop-yes and prop-edit
// were only ever wired inside the story overlay, so on this screen "Approve & send" and
// "Edit first" were dead buttons — clicking them did nothing at all.
function wireProposalActions(scope, agentId, after) {
  const el = scope || document;
  el.querySelectorAll(".prop-yes").forEach(b => b.onclick = async () => {
    const id = b.dataset.id;
    settleProposalCard(id, "Sending…", true);
    const r = await api("POST", `/api/u/agents/${agentId}/approve-actions`,
                        { pending_ids: [id] });
    settleProposalCard(id, r.ok ? "✓ Done — this was sent"
                                : "Couldn't send — try again from the agent's page", r.ok);
    if (after) after(r);
  });
  el.querySelectorAll(".prop-no").forEach(b => b.onclick = async () => {
    const id = b.dataset.id;
    const reason = await askWhy();
    if (reason === null) return;                     // cancelled — nothing rejected
    settleProposalCard(id, "Rejected — this won't be sent", false);
    await api("POST", `/api/u/pending/${id}/reject`, { reason });
    if (after) after({ ok: true });
  });
}

// The card has always said "Reject asks one line: why?" and nothing ever asked. A
// rejection with no reason tells the agent an action was unwanted and never what was
// wrong with it — and the reason is what the next proposal could actually use. Optional
// on purpose: a required field would just collect "no" from people in a hurry.
function askWhy() {
  return new Promise(resolve => {
    const el = document.createElement("div");
    el.className = "pop-back";
    el.innerHTML = `<div class="pop pop-confirm" role="dialog" aria-modal="true">
        <div class="pop-title">Why not?</div>
        <p class="cf-body">One line is enough. The agent uses it so the same thing
          doesn't come back next run — and it becomes a lesson it remembers.</p>
        <input type="text" id="whyText" class="cf-input" autocomplete="off"
               placeholder="e.g. the ship date isn't confirmed yet">
        <div class="cf-acts">
          <button class="btn-secondary" id="whyNo">Cancel</button>
          <button class="btn-primary" id="whyGo">Reject</button>
        </div>
      </div>`;
    document.body.appendChild(el);
    const input = el.querySelector("#whyText");
    const done = (v) => {
      document.removeEventListener("keydown", onKey);
      el.remove();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === "Escape") done(null);
      if (e.key === "Enter") done(input.value.trim());
    };
    el.onclick = (e) => { if (e.target === el) done(null); };
    el.querySelector("#whyNo").onclick = () => done(null);
    el.querySelector("#whyGo").onclick = () => done(input.value.trim());
    document.addEventListener("keydown", onKey);
    input.focus();
  });
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
  const ok = await confirmDestructive({
    title: `Stop asking approval for ${app}?`,
    body: `Future ${app} actions run automatically, without showing you first. You can turn approval back on from this agent's page.`,
    verb: "Stop asking", word: null, tone: "primary",
  });
  if (!ok) return false;
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
      <button class="btn-primary btn-sm dec-submit" data-id="${esc(d.id)}">Answer</button>
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

/* Typical run length, in whole minutes, from the run log's own timestamps. Median
   rather than mean so one pathological run (a stop waiting hours on a human) doesn't
   describe the rest. null when nothing has a measurable duration, which reads as
   "don't show the card" — better than a confident 0. */
function runMinutes(runs) {
  const spans = (runs || []).map(r => {
    const a = Date.parse(r.started_at || r.ran_at || ""), b = Date.parse(r.ended_at || "");
    return (isFinite(a) && isFinite(b) && b > a) ? (b - a) / 60000 : null;
  }).filter(v => v !== null).sort((x, y) => x - y);
  if (!spans.length) return null;
  const mid = spans[Math.floor(spans.length / 2)];
  return Math.max(1, Math.round(mid));
}

/* "Today 6:04" / "Mon 6:02" — the frame's own shorthand. */
function whenShort(ts) {
  const d = new Date(ts || "");
  if (isNaN(d)) return "—";
  const now = new Date();
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return `Today ${time}`;
  const days = (now - d) / 86400000;
  if (days < 7) return `${d.toLocaleDateString(undefined, { weekday: "short" })} ${time}`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}


/* Frame 21 closes the Permissions card with one line about when the agent stops and
   asks — "Escalation: team rules + 1 override". The frame's phrasing assumes a team
   tier that does not exist yet, so this states what IS known: how many stop-rules the
   worker set at the safety step, and the first of them. Silent when none were set,
   because "Escalation: none" reads as a missing feature rather than a choice. */
function escalationLine(data) {
  const raw = ((data && data.agent && data.agent.rules) || "").trim();
  if (!raw) return "";
  const rules = raw.split(/\n+/).map(r => r.replace(/^[-•*\s]+/, "").trim()).filter(Boolean);
  if (!rules.length) return "";
  const more = rules.length > 1 ? ` + ${rules.length - 1} more` : "";
  return `<div class="sub muted" style="margin-top:6px;">Escalation: ${
    esc(rules[0])}${more} — it stops and asks you.</div>`;
}

function lessonConfirmCard(agentId, c) {
  return `<div class="card lesson-card" style="margin-top:8px;border-left:3px solid var(--accent,#5b8def);">
    <div class="sub muted" style="margin-bottom:4px;">A similar agent learned this — apply it here?</div>
    <div class="title" style="font-size:14px;">${esc(c.lesson || "")}</div>
    <div class="row" style="gap:8px;margin-top:10px;">
      <button class="btn-primary btn-sm lesson-yes" data-agent="${esc(agentId)}" data-id="${esc(c.decision_id)}">Use it</button>
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

// The app shell (frame 14): a persistent left sidebar plus the content column.
//
// The sidebar is built once and then STAYS. `tb.paint(content)` swaps only <main>, so
// changing tabs re-parses one column instead of the whole page — the sidebar never
// blanks and never re-lays-out, which is most of what made navigation read as a reload.
// The first paint (or the first after the wizard/review overlays clear #root) has no
// shell to reuse, so it writes the whole thing; the shell's opening tags are left open
// and innerHTML closes them at the end of the fragment, which is defined behaviour.
const NAV = [
  // Home paints from cache and revalidates behind it, so this no longer drops the
  // cache — doing that forced a blocking refetch on every visit to the landing screen.
  { key: "home", label: "Home", go: () => renderHome() },
  { key: "messages", label: "Messages", go: () => renderMessages() },
  { key: "groups", label: "Groups", go: () => renderGroups() },
  { key: "sharing", label: "Sharing", go: () => renderSettings() },
  // Settings used to open renderSettings too — two sidebar items, one screen. Sharing
  // (25) keeps that screen; Settings (18) is its own shell.
  { key: "settings", label: "Settings", go: () => renderSettingsShell() },
];

// Bumped by every screen paint. A render that awaits data captures this first and
// compares afterwards, so a slow response cannot paint over a screen you have since
// navigated to.
let SCREEN = 0;

// The <main> of a shell that is already on screen, or null if there is nothing to reuse.
function liveMain() {
  const shell = root.firstElementChild;
  return shell && shell.classList.contains("shell") ? shell.querySelector(".main") : null;
}

// Bring an already-rendered sidebar up to date: which item reads as current, and the
// unread count beside Messages. Cheaper than rebuilding it, and it keeps the highlight
// under the pointer from flickering off and back on during a tab change.
function syncNav(activeKey, unread) {
  document.querySelectorAll(".nav-item").forEach(o => {
    const on = o.dataset.nav === activeKey;
    o.classList.toggle("active", on);
    const d = o.querySelector(".nav-dot");
    if (d) d.classList.toggle("on", on);
    if (o.dataset.nav === "messages" && typeof unread === "number") {
      const b = o.querySelector(".ct-badge");
      if (unread > 0) {
        const txt = unread > 9 ? "9+" : String(unread);
        if (b) b.textContent = txt;
        else o.insertAdjacentHTML("beforeend", `<span class="ct-badge">${txt}</span>`);
      } else if (b) b.remove();
    }
  });
}

// Painted the instant a nav item is clicked, before the screen's fetch resolves, so the
// click has a visible consequence rather than leaving the previous screen sitting there
// looking frozen. Panel-shaped, so real content lands in place instead of shifting it.
function mainSkeleton() {
  return `<div class="panel sk-panel" aria-busy="true">
    <div class="sk sk-head"></div>
    <div class="sk sk-line"></div><div class="sk sk-line short"></div>
    <div class="sk-rows">${`<div class="sk sk-row"></div>`.repeat(3)}</div>
  </div>`;
}

// Called as the first statement of a render that has to fetch before it can paint — it
// runs before the first await, so the skeleton is on screen in the same tick as the
// click. A render that can paint from cache skips this and never flashes one.
function busy() {
  const main = liveMain();
  if (main) main.innerHTML = mainSkeleton();
}

function agentsTopbar(unread, backFn, needsYou, active) {
  const ct = (n) => n > 9 ? "9+" : n;
  const initial = (S.user && S.user.email || "?").trim().charAt(0).toUpperCase();
  const navKey = active || "home";
  const items = NAV.map(n => {
    const on = n.key === navKey;
    const badge = (n.key === "messages" && unread)
      ? `<span class="ct-badge">${ct(unread)}</span>` : "";
    return `<button class="nav-item${on ? " active" : ""}" data-nav="${n.key}">
      <span class="nav-dot${on ? " on" : ""}"></span>${n.label}${badge}</button>`;
  }).join("");
  const back = backFn
    ? `<button id="agBack" class="btn-link" style="margin-bottom:6px;">← back</button>` : "";
  const shell = `<div class="shell"><aside class="sidebar">
        ${brandLockup()}
        ${items}
        <div class="side-foot">
          <button id="navNew" class="btn-accent full">+ New workflow</button>
          <div class="user-chip"><span class="user-av">${esc(initial)}</span>
            <span class="user-mail">${esc(S.user ? S.user.email : "")}</span>
            <button id="logout" class="btn-link" title="Sign out">Sign out</button></div>
        </div>
      </aside><main class="main">
      ${back}`;
  return {
    html: shell,
    // Write this screen's content into the shell, reusing the sidebar when one is up.
    paint(content) {
      SCREEN++;
      const main = liveMain();
      if (!main) { root.innerHTML = shell + content; return; }
      syncNav(navKey, unread);
      main.innerHTML = back + content;
      main.scrollTop = 0;
    },
    wire() {
      root.className = "";                        // the shell owns layout, not .wrap
      document.querySelectorAll(".nav-item").forEach(b => {
        b.onclick = () => {
          // Acknowledge the click before the screen's data arrives: move the highlight,
          // then drop a skeleton into <main>. Waiting for the fetch to come back before
          // showing anything is what made the sidebar feel sluggish.
          syncNav(b.dataset.nav, unread);
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
  busy();
  const home = S.igHome || await loadHome();
  const { data } = await api("GET", "/api/u/consent");
  const c = data || { contribute_scope: "global", consume_scope: "global" };
  const tb = agentsTopbar(home.unread_total, null, home.needs_you_total, "sharing");
  // Frame 25's own words, which are shorter and say the load-bearing part — lessons
  // only — before anything else.
  const overrideLine = (a) => {
    const c = a.lesson_contribute_scope, u = a.lesson_consume_scope;
    if (!c && !u) return "follows your default";
    const bits = [];
    if (c === "owner") bits.push("contributes nothing");
    else if (c === "team") bits.push("contributes to your team");
    else if (c === "global") bits.push("contributes to everyone");
    if (u === "owner") bits.push("uses only its own");
    else if (u === "team") bits.push("asks first");
    else if (u === "global") bits.push("auto-adopts");
    return bits.join(" · ") || "follows your default";
  };
  tb.paint(`
    <div class="panel">
      <h2>Sharing</h2>
      <p class="lead">Teammates' agents can learn from yours — <b>lessons only</b>, never your
        emails, data, or recordings. This is your account default; any agent can override it.</p>
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
      <p class="sub muted" style="margin-top:12px;">"Anonymized" means names, emails, amounts and ids are stripped and a second privacy check runs before anything leaves your account. A shared lesson only ever guides someone else's agent after they confirm it once.</p>
    </div>
    ${(home.agents || []).length ? `<div class="panel">
      <h2 style="font-size:16px;">Per agent</h2>
      <p class="sub muted" style="margin-top:2px;">Anything set here wins over the dials above.
        Change one on its own page.</p>
      <div class="sh-rows">${home.agents.map(a => `
        <button class="sh-row" data-agent="${esc(a.agent_id)}">
          <span class="sh-chip" style="background:${esc(agentColor(a))};"></span>
          <span class="sh-name">${esc(a.name)}</span>
          <span class="sh-ov">${esc(overrideLine(a))} ›</span>
        </button>`).join("")}</div>
    </div>` : ""}`);
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
  // Each row opens that agent's page, which is where its override actually lives —
  // rather than duplicating the control in two places that could then disagree.
  root.querySelectorAll(".sh-row").forEach(b =>
    b.onclick = () => renderProfile(b.dataset.agent));
}

/* ---------------- SETTINGS (18) ---------------- */

// Five panes behind a second column. Panels carry data-pane and an untagged panel
// defaults to the first, so a pane added later can't silently vanish — the same trick
// the profile tabs use.
const SETTINGS_PANES = [
  ["account", "Account"], ["team", "Team"], ["notifications", "Notifications"],
  ["privacy", "Privacy &amp; data"], ["apps", "Connected apps"],
  ["appearance", "Appearance"],
];

async function renderSettingsShell(pane) {
  clearTimers();
  busy();
  const want = pane || "account";
  const home = S.igHome || await loadHome();
  const tb = agentsTopbar(home.unread_total, null, home.needs_you_total, "settings");
  const nav = SETTINGS_PANES.map(([k, label]) =>
    `<button class="set-nav${k === want ? " on" : ""}" data-pane="${k}">${label}</button>`).join("");
  tb.paint(`
    <div class="set-wrap">
      <div class="set-col">
        <div class="set-title">Settings</div>
        ${nav}
      </div>
      <div class="set-main">
        <div class="eyebrow" id="setEyebrow">${
          (SETTINGS_PANES.find(p => p[0] === want) || SETTINGS_PANES[0])[1]}</div>
        <div id="setPane"><p class="muted">Loading…</p></div>
      </div>
    </div>`);
  tb.wire();
  root.querySelectorAll(".set-nav").forEach(b =>
    b.onclick = () => renderSettingsShell(b.dataset.pane));
  ({ account: setAccount, team: setTeam, notifications: setNotifications,
     privacy: setPrivacy, apps: setApps, appearance: setAppearance }[want] || setAccount)();
}

async function setAccount() {
  const { data } = await api("GET", "/api/u/profile");
  const p = data || {};
  const initial = (p.email || "?").trim().charAt(0).toUpperCase();
  const sub = [p.email, p.org_name].filter(Boolean).join(" · ");
  $("#setPane").innerHTML = `
    <div class="set-card">
      <span class="set-av">${esc(initial)}</span>
      <div class="grow">
        <div class="set-name">${esc(p.display_name || p.email || "You")}</div>
        <div class="set-sub">${esc(sub)}</div>
      </div>
      <button class="btn-secondary btn-sm" id="setEdit">Edit</button>
    </div>`;
  $("#setEdit").onclick = () => editAccount(p);
}

// Reuses the .pop modal every other dialog uses rather than inventing an inline form.
function editAccount(p) {
  const el = document.createElement("div");
  el.className = "pop-back";
  el.innerHTML = `<div class="pop" role="dialog" aria-modal="true">
      <div class="row"><div class="pop-title">Edit your details</div>
        <button class="pop-x" aria-label="Close">✕</button></div>
      <div class="field"><span class="label">Name</span>
        <input id="acName" type="text" value="${esc(p.display_name || "")}" placeholder="Your name"></div>
      <div class="field"><span class="label">Organisation</span>
        <input id="acOrg" type="text" value="${esc(p.org_name || "")}" placeholder="Where you work"></div>
      <p class="pop-note">Your email is how you sign in and can't be changed here.</p>
      <div class="cf-acts">
        <button class="btn-secondary" id="acNo">Cancel</button>
        <button class="btn-primary" id="acYes">Save</button>
      </div>
    </div>`;
  document.body.appendChild(el);
  const close = () => el.remove();
  el.onclick = (e) => { if (e.target === el) close(); };
  el.querySelector(".pop-x").onclick = close;
  el.querySelector("#acNo").onclick = close;
  el.querySelector("#acYes").onclick = async () => {
    const btn = el.querySelector("#acYes");
    btn.disabled = true; btn.textContent = "Saving…";
    await api("POST", "/api/u/profile", {
      display_name: el.querySelector("#acName").value,
      org_name: el.querySelector("#acOrg").value,
    });
    close();
    setAccount();
  };
  el.querySelector("#acName").focus();
}

// Frame 30. No team yet is a normal state, not an empty one — a team comes into being
// when you invite somebody, so the pane leads with the invite rather than a Create button.
async function setTeam() {
  const { data } = await api("GET", "/api/u/team");
  const d = data || {};
  // "owner" is what the database trigger writes for whoever created the team, and it
  // outranks admin. Checking for exactly "admin" hid the invite UI from the one person
  // guaranteed to be entitled to it — the server-side check learned this and this one
  // did not, which is the same rule living in two places disagreeing.
  const ADMIN_ROLES = ["admin", "owner"];
  const admin = !d.team || ADMIN_ROLES.includes(d.team.role);
  const memberRow = (m) => `
    <div class="set-card">
      <span class="set-av set-av-sm">${esc((m.email || "?").charAt(0).toUpperCase())}</span>
      <div class="grow">
        <div class="set-row-t">${esc(m.email)}${m.is_you ? " · you" : ""}</div>
        <div class="set-sub">${m.agents_live} agent${m.agents_live === 1 ? "" : "s"} live</div>
      </div>
      <span class="pill ${ADMIN_ROLES.includes(m.role) ? "pill-on" : "pill-off"}">${esc((m.role || "member").toUpperCase())}</span>
      ${admin && !m.is_you ? `<button class="btn-link tm-del" data-id="${esc(m.user_id)}"
        style="color:var(--danger);">Remove</button>` : ""}
    </div>`;
  const inviteRow = (i) => `
    <div class="set-card">
      <span class="set-av set-av-sm tm-pending">${esc((i.email || "?").charAt(0).toUpperCase())}</span>
      <div class="grow">
        <div class="set-row-t">${esc(i.email)}</div>
        <div class="set-sub">Invited ${esc(threadStamp(i.invited_at))} — pending</div>
      </div>
      ${admin ? `<button class="btn-secondary btn-sm tm-resend" data-id="${esc(i.id)}">Resend</button>
      <button class="btn-link tm-cancel" data-id="${esc(i.id)}" style="color:var(--ink-2);">Cancel</button>` : ""}
    </div>`;
  // An invite addressed to YOU. Joining is never implicit — you may already own a team
  // (one is created the moment you press Invite), and being moved out of it silently is
  // not something that should happen to somebody.
  const myInviteRow = (i) => `
    <div class="set-card">
      <span class="set-av set-av-sm tm-pending">${esc((i.team_name || "?").charAt(0).toUpperCase())}</span>
      <div class="grow">
        <div class="set-row-t">${esc(i.team_name)} invited you</div>
        <div class="set-sub">As ${esc(i.role || "member")} · ${esc(threadStamp(i.invited_at))}</div>
      </div>
      <button class="btn-primary btn-sm tm-accept" data-id="${esc(i.id)}">Join</button>
    </div>`;
  $("#setPane").innerHTML = `
    ${(d.my_invites || []).length ? `<div class="stack" style="margin-bottom:16px;">
      ${d.my_invites.map(myInviteRow).join("")}
      <p id="tmInvMsg" class="msg err"></p></div>` : ""}
    ${d.team ? `<p class="set-sub" style="margin:-8px 0 14px;">${esc(d.team.name)} ·
      ${d.members.length} member${d.members.length === 1 ? "" : "s"} — lesson sharing set
      to "my team" runs inside this group.</p>` : `
      <p class="set-sub" style="margin:-8px 0 14px;">You don't have a team yet. Inviting
      someone creates one, with you as its admin.</p>`}
    ${d.email_configured ? "" : `<p class="notice">Email isn't configured, so an invite is
      recorded but nothing is sent. Set RESEND_API_KEY and EMAIL_FROM, or pass the link on
      yourself.</p>`}
    <div class="stack">${(d.members || []).map(memberRow).join("")}${(d.invites || []).map(inviteRow).join("")}</div>
    ${admin ? `<div class="tm-invite">
      <div class="set-row-t">Invite someone</div>
      <div class="row" style="gap:8px;margin-top:8px;flex-wrap:wrap;">
        <input type="text" id="tmEmail" placeholder="name@company.com" style="flex:1;min-width:200px;padding:9px 12px;">
        <select id="tmRole" style="padding:9px 10px;"><option value="member">Member</option><option value="admin">Admin</option></select>
        <button class="btn-primary btn-sm" id="tmGo">Send invite</button>
      </div>
      <p class="set-sub" style="margin-top:8px;">Members build their own agents and choose
        their own sharing. Admins also manage members and invites.</p>
      <p id="tmMsg" class="msg err"></p>
    </div>` : `<p class="notice">Only an admin can invite or remove people.</p>`}`;
  const post = async (path, body) => {
    const r = await api("POST", path, body || {});
    if (!r.ok) {
      const m = $("#tmMsg") || $("#tmInvMsg");
      if (m) m.textContent = { bad_email: "that doesn't look like an email address",
        thats_you: "that's your own address", not_admin: "only an admin can do that",
        last_admin: "there'd be no admin left — promote someone first",
        already_in_a_team: "you're already in a team with other people in it — leave that one first",
      }[r.data && r.data.error] || friendly(r.data && r.data.error);
      return false;
    }
    renderSettingsShell("team");
    return true;
  };
  const go = $("#tmGo");
  if (go) go.onclick = () => post("/api/u/team/invite", {
    email: $("#tmEmail").value, role: $("#tmRole").value });
  root.querySelectorAll(".tm-accept").forEach(b => b.onclick = () =>
    post(`/api/u/team/invites/${b.dataset.id}/accept`));
  root.querySelectorAll(".tm-resend").forEach(b => b.onclick = () =>
    post(`/api/u/team/invites/${b.dataset.id}/resend`));
  root.querySelectorAll(".tm-cancel").forEach(b => b.onclick = () =>
    post(`/api/u/team/invites/${b.dataset.id}/cancel`));
  root.querySelectorAll(".tm-del").forEach(b => b.onclick = async () => {
    if (!await confirmDestructive({
      title: "Remove them from the team?",
      body: "Their agents and data are untouched — they just stop sharing lessons with your team, and stop seeing yours.",
      verb: "Remove", word: null, tone: "primary" })) return;
    post(`/api/u/team/members/${b.dataset.id}/remove`);
  });
}

async function setNotifications() {
  const { data } = await api("GET", "/api/u/notifications");
  const p = data || {};
  const rows = [
    ["notify_morning_brief", "Morning brief notification",
     "One summary when overnight runs finish"],
    ["notify_agent_blocked", "Ping me when an agent is blocked",
     "Questions and approvals, as they happen"],
    ["notify_weekly_digest", "Weekly digest email", "Runs, approvals, time saved"],
  ];
  // Saying so is better than switches that quietly do nothing: without a key and a
  // verified sending domain, nothing can leave the building whatever these are set to.
  const off = p.email_configured ? "" : `<p class="notice">Email isn't configured yet, so
    these are saved but nothing is sent. Set RESEND_API_KEY and EMAIL_FROM to turn
    delivery on — the in-app messages and badges work either way.</p>`;
  $("#setPane").innerHTML = `${off}
    <div class="set-card set-rows">${rows.map(([k, t, d]) => `
      <button class="set-row" data-key="${k}" aria-pressed="${!!p[k]}">
        <div class="grow"><div class="set-row-t">${t}</div><div class="set-sub">${d}</div></div>
        <span class="tgl${p[k] ? " on" : ""}"><i></i></span>
      </button>`).join("")}</div>`;
  root.querySelectorAll(".set-row").forEach(b => b.onclick = async () => {
    const key = b.dataset.key, next = b.getAttribute("aria-pressed") !== "true";
    b.setAttribute("aria-pressed", String(next));
    b.querySelector(".tgl").classList.toggle("on", next);
    // Saves on toggle, like the sharing dials — a Save button would imply an unsaved
    // state that doesn't exist.
    await api("POST", "/api/u/notifications", { [key]: next });
  });
}

async function setPrivacy() {
  const home = S.igHome || await loadHome();
  const agents = (home.agents || []).filter(a => a.capture_present);
  $("#setPane").innerHTML = `
    <div class="danger-card">
      <div class="set-name" style="color:var(--danger);">Delete all capture data</div>
      <div class="set-sub">Removes every recording from your device and the cloud. Your
        agents keep working; they just can't be retrained from it.</div>
      <div class="set-sub" style="margin-top:6px;">${agents.length
        ? `${agents.length} agent${agents.length === 1 ? "" : "s"} still ${
            agents.length === 1 ? "has" : "have"} a recording.`
        : "Nothing recorded is being kept right now."}</div>
      <button class="btn-danger btn-sm" id="setWipe" style="margin-top:12px;"
        ${agents.length ? "" : "disabled"}>Delete…</button>
      <p id="setWipeMsg" class="msg"></p>
    </div>`;
  $("#setWipe").onclick = async () => {
    const ok = await confirmDestructive({
      title: "Delete every recording?",
      body: `This removes the capture data behind ${agents.length} agent${
        agents.length === 1 ? "" : "s"}, from your device and the cloud, permanently. The
        agents keep running — they just can't be retrained from it.`,
    });
    if (!ok) return;
    const btn = $("#setWipe");
    btn.disabled = true; btn.textContent = "Deleting…";
    for (const a of agents) {
      await api("POST", `/api/u/agents/${a.agent_id}/delete-capture-data`, {});
    }
    S.igHome = null;
    renderSettingsShell("privacy");
  };
}


/* Frame 26 ends each connected-app row with "last used 7:04 AM". Nothing records a
   per-app timestamp, and inventing one would be a claim about someone's account
   activity — but the agents that USE the app do have run history, and the most recent
   of those runs is when the app was last touched on your behalf. That is the same fact
   the frame is after, derived rather than asserted. Silent when no agent using it has
   run, because "last used never" is noise. */
function lastUsedSuffix(app) {
  const home = S.igHome;
  if (!home || !(app.used_by || []).length) return "";
  const names = new Set(app.used_by);
  let latest = null;
  for (const ag of (home.agents || [])) {
    if (!names.has(ag.name)) continue;
    for (const st of (ag.stories || [])) {
      const t = Date.parse(st.ran_at || "");
      if (isFinite(t) && (latest === null || t > latest)) latest = t;
    }
  }
  return latest === null ? "" : ` · last used ${whenShort(new Date(latest).toISOString())}`;
}

async function setApps() {
  // Frame 26 wants a last-used time and a Revoke action. Last-used IS derivable — the
  // agents using an app have run history, and their most recent run is when the app was
  // last touched on your behalf. Revoke genuinely is not: it needs a Pipedream call that
  // connect_client does not expose, so the screen says so rather than pretending.
  const { data } = await api("GET", "/api/u/sessions");
  const sess = ((data || {}).sessions || [])[0];
  if (!sess) {
    $("#setPane").innerHTML = `<p class="muted">No connected apps yet — you'll connect
      them while setting up your first workflow.</p>`;
    return;
  }
  const c = await uget(sess.id, "/connection-data");
  const apps = ((c.data || {}).record || {}).apps || {};
  const slugs = Object.keys(apps);
  $("#setPane").innerHTML = slugs.length ? `
    <div class="stack">${slugs.map(s => {
      const a = apps[s];
      const expired = a.connected && !a.usable;
      return `<div class="set-card${expired ? " set-card-warn" : ""}">
        <span class="set-av set-av-sm">${esc((a.label || s).charAt(0).toUpperCase())}</span>
        <div class="grow">
          <div class="set-row-t">${esc(a.label || s)}</div>
          <div class="set-sub${expired ? " warn" : ""}">${expired
            ? `Session expired — ${(a.used_by || []).length
                ? esc((a.used_by || []).join(", ")) + " " +
                  ((a.used_by || []).length === 1 ? "is" : "are") + " paused until you reconnect"
                : "reconnect to let its agents run again"}`
            : (a.usable ? "Connected · outbound actions are staged for your approval"
                        : "Not connected")}</div>
          ${!expired && (a.used_by || []).length
            ? `<div class="set-sub">${esc((a.used_by || []).join(", "))}${
                lastUsedSuffix(a)}</div>` : ""}
        </div>
        <span class="pill ${a.usable ? "pill-on" : (expired ? "pill-warn" : "pill-off")}">${
          a.usable ? "CONNECTED" : (expired ? "EXPIRED" : "OFF")}</span>
      </div>`;
    }).join("")}</div>
    <p class="notice">Reconnecting and revoking happen in the setup flow for now —
       there is no revoke endpoint yet, and a button that only looks like one would be
       worse than none.</p>`
    : `<p class="muted">No apps connected yet.</p>`;
}

function setAppearance() {
  // System + Light only, deliberately. A dark option needs a second complete token set
  // in app.css plus an audit of the hard-coded hexes in inline styles; offering a Dark
  // that half-works would be worse than not offering it.
  $("#setPane").innerHTML = `
    <div class="set-card"><div class="grow">
      <div class="set-row-t">Theme</div>
      <div class="set-sub">palza follows your system's light setting.</div>
    </div><span class="pill pill-off">SYSTEM</span></div>
    <p class="notice">A dark theme is coming — it needs a full second palette, and a
      half-finished one looks worse than none.</p>`;
}

/* ---------------- GROUP CHAT: agents as contacts in a group ---------------- */
// Mirrors registry._AGENT_COLORS — the palette the server falls back to when an agent
// has no explicit color, so the picker offers exactly the same choices.
const AGENT_COLORS = ["#F26A1B", "#2D7FF9", "#12B886", "#E8590C", "#7048E8",
                      "#E64980", "#1098AD", "#F59F00", "#4263EB", "#0CA678"];

// Frame 24's group row wants "n agents · active {t}" and a coral dot when a member is
// blocked, but /api/u/groups returns only {id, name, members:[{agent_id,name,color}]} —
// no timestamps, no status. Both are derivable from the home brief we already have, so
// this cross-references it rather than growing the endpoint: "active" is the most recent
// run among the members, and "blocked" is a member that needs you (a question it can't
// settle, or an action waiting on your sign-off) — which is exactly what the dot means.
function groupStatus(group, agents) {
  const by = {};
  (agents || []).forEach(a => { by[a.agent_id] = a; });
  let last = null, lastMs = 0, blocked = false;
  for (const m of group.members || []) {
    const a = by[m.agent_id];
    if (!a) continue;
    if (a.needs_you > 0) blocked = true;
    for (const s of a.stories || []) {
      const ms = s.ran_at ? Date.parse(s.ran_at) : NaN;
      if (ms && ms > lastMs) { lastMs = ms; last = s.ran_at; }
    }
  }
  return { last, blocked };
}

async function renderGroups() {
  clearTimers();
  busy();
  const home = S.igHome || await loadHome();
  const { data } = await api("GET", "/api/u/groups");
  const groups = (data && data.groups) || [];
  const agents = home.agents || [];
  const tb = agentsTopbar(home.unread_total, null, home.needs_you_total, "groups");

  // Stacked color tiles (24): the group's members read as one object at a glance. Four
  // is where the overlap stops being legible, so the rest become a "+n" tile.
  const tiles = (members) => {
    const shown = members.slice(0, 4);
    const rest = members.length - shown.length;
    return `<span class="grp-tiles">${shown.map(m =>
      `<span class="grp-tile" style="background:${esc(m.color)};" title="${esc(m.name)}"></span>`
    ).join("")}${rest ? `<span class="grp-tile grp-tile-more">+${rest}</span>` : ""}</span>`;
  };

  const groupRows = groups.map(g => {
    const { last, blocked } = groupStatus(g, agents);
    const n = (g.members || []).length;
    const when = last
      ? "active " + new Date(last).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
      : "no runs yet";
    return `<button class="grp-card" data-id="${esc(g.id)}">
      ${tiles(g.members || [])}
      <span class="grp-mid">
        <span class="grp-name">${esc(g.name)}</span>
        <span class="grp-sub">${n} agent${n === 1 ? "" : "s"} · ${esc(when)}</span>
      </span>
      ${blocked ? `<span class="flag-dot" title="A member needs you"></span>` : ""}
    </button>`;
  }).join("");

  const agentOpts = agents.map(a => `
    <button class="grp-pick" data-id="${esc(a.agent_id)}" aria-pressed="false">
      <span class="grp-box"></span>
      <span class="grp-chip" style="background:${esc(agentColor(a))};"></span>
      <span class="grp-pick-name">${esc(a.name)}</span>
    </button>`).join("");

  tb.paint(`
    <div class="grp-head">
      <h1 class="grp-title">Groups</h1>
      <button id="grpNew" class="btn-accent btn-sm">+ New group</button>
    </div>
    <p class="lead">Talk to several agents at once. "What did y'all do today?" gets each one's answer; @mention one to give it a task.</p>
    <div class="stack" style="margin-top:14px;">${groupRows
      || `<p class="muted">No groups yet — "+ New group" puts a few agents in one conversation.</p>`}</div>
    <div class="grp-new" id="grpForm" hidden>
      <div class="grp-new-title">New group</div>
      <input type="text" id="grpName" class="grp-input" placeholder="Group name (e.g. Operations)" autocomplete="off">
      <div class="grp-picks">${agentOpts || `<p class="muted">No agents yet.</p>`}</div>
      <button class="btn-primary full" id="grpCreate">Create group</button>
    </div>`);
  tb.wire();

  root.querySelectorAll(".grp-card").forEach(b => b.onclick = () =>
    renderGroupChat(groups.find(x => x.id === b.dataset.id)));

  const form = $("#grpForm");
  $("#grpNew").onclick = () => {
    form.hidden = !form.hidden;
    if (!form.hidden) $("#grpName").focus();
  };
  // The checklist is buttons rather than <input type=checkbox> so the tick can carry
  // frame 24's filled-square treatment; aria-pressed keeps it announced correctly.
  root.querySelectorAll(".grp-pick").forEach(b => b.onclick = () => {
    const on = b.getAttribute("aria-pressed") === "true";
    b.setAttribute("aria-pressed", on ? "false" : "true");
    b.classList.toggle("on", !on);
  });
  const create = async () => {
    const name = ($("#grpName").value || "").trim();
    if (!name) { $("#grpName").focus(); return; }
    const ids = Array.from(root.querySelectorAll(".grp-pick.on")).map(b => b.dataset.id);
    const btn = $("#grpCreate");
    btn.disabled = true; btn.textContent = "Creating…";
    await api("POST", "/api/u/groups", { name, agent_ids: ids });
    renderGroups();
  };
  $("#grpCreate").onclick = create;
  $("#grpName").onkeydown = (e) => { if (e.key === "Enter") create(); };
}


/* s17's "+ Add agent". _update_group has accepted agent_ids since groups landed, so
   this is UI only — a group's membership was simply fixed at creation because nothing
   ever offered to change it. Only agents NOT already in the group are listed; an
   "add" list that includes the members is a list of things that do nothing. */
async function wireGroupAdd(group, redraw) {
  const btn = $("#grpAdd"), box = $("#grpAddBox");
  if (!btn || !box) return;
  btn.onclick = async () => {
    if (box.style.display !== "none") { box.style.display = "none"; return; }
    box.style.display = "";
    box.innerHTML = `<p class="sub muted">Loading…</p>`;
    const home = S.igHome || await loadHome();
    const inGroup = new Set((group.members || []).map(m => m.agent_id));
    const rest = (home.agents || []).filter(a => !inGroup.has(a.agent_id));
    if (!rest.length) {
      box.innerHTML = `<p class="sub muted">Every agent you have is already in this group.</p>`;
      return;
    }
    box.innerHTML = `<div class="row" style="gap:6px;flex-wrap:wrap;">${rest.map(a =>
      `<button class="chip grp-add-one" data-id="${esc(a.agent_id)}">${
        esc(a.name || a.agent_id)}</button>`).join("")}</div>`;
    box.querySelectorAll(".grp-add-one").forEach(c => c.onclick = async () => {
      c.disabled = true; c.textContent = "Adding…";
      const ids = Array.from(inGroup).concat([c.dataset.id]);
      // POST, not PATCH: _update_group is routed inside the POST block at
      // /groups/<id> alongside create and delete.
      const { ok, data } = await api("POST", `/api/u/groups/${group.id}`,
                                     { agent_ids: ids });
      if (!ok) { c.textContent = friendly((data || {}).error || "could not add"); return; }
      const fresh = ((data || {}).groups || []).find(g => g.id === group.id);
      if (fresh) { group.members = fresh.members; }
      box.style.display = "none";
      redraw();
    });
  };
}

function renderGroupChat(group) {
  clearTimers();
  if (!group) { renderGroups(); return; }
  const msgs = [];   // {who:'you'|'sys'|agentId, name, color, text, staged}
  // Frame 17 renders @mentions in light blue inside the ink bubbles, so directing a
  // question at one agent looks different from asking the room. Escaped FIRST, then the
  // mention is wrapped — the pattern only ever matches text esc() has already made safe.
  const mentions = (text) => esc(text).replace(/@([\w][\w .'-]{0,38}\w)/g,
    (_, n) => `<span class="mention">@${n}</span>`);
  const bubble = (m) => m.who === "you"
    ? `<div class="row" style="justify-content:flex-end;margin-top:10px;"><div class="card grp-you">${mentions(m.text)}</div></div>`
    : `<div style="margin-top:10px;">
         <div class="sub ag-name" style="color:${esc(m.color)};">${esc(m.name)}</div>
         <div class="card" style="border-left:3px solid ${esc(m.color)};max-width:82%;">${mentions(m.text)}
           ${m.staged ? `<div class="sub muted" style="margin-top:4px;">📋 ${m.staged} action${m.staged > 1 ? "s" : ""} staged — approve on the agent's page.</div>` : ""}
         </div></div>`;
  const draw = () => {
    const tb = agentsTopbar(0, renderGroups, 0, "groups");
    tb.paint(`
      <div class="panel">
        <div class="row" style="gap:8px;align-items:baseline;"><h2 style="margin:0;">${esc(group.name)}</h2>
          <span class="sub muted">${group.members.map(m => `<span style="color:${esc(m.color)};">${esc(m.name)}</span>`).join(", ") || "no members"}</span>
          <button class="btn-link btn-sm" id="grpAdd" style="margin-left:auto;">+ Add agent</button></div>
        <div id="grpAddBox" style="display:none;margin-top:8px;"></div>
        <div id="grpMsgs" style="margin-top:12px;min-height:140px;max-height:60vh;overflow:auto;">${
          msgs.map(bubble).join("") || `<p class="muted">Try "what did y'all get up to today?" — or @${esc((group.members[0] || {}).name || "agent")} to direct one.</p>`}</div>
        <div class="row" style="gap:6px;margin-top:12px;">
          <input type="text" id="grpInput" placeholder="Message the group…  (@name to direct one)" style="flex:1;padding:8px;">
          <button class="btn-primary btn-sm" id="grpSend">Send</button>
        </div>
      </div>`);
    tb.wire();
    wireGroupAdd(group, draw);
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
  busy();
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
  tb.paint(`
    <div class="panel">
      <div class="spread"><h2>Your agents</h2>
        <button id="recAnother" class="btn-secondary btn-sm">Record another workflow</button></div>
      <p class="lead">Every workflow you've automated. Open one to see its runs, rules and lessons.</p>
      <div class="stack" style="margin-top:14px;">${rows || `<p class="muted">No agents yet.</p>`}</div>
    </div>`);
  tb.wire();
  const ra = $("#recAnother"); if (ra) ra.onclick = recordAnother;
  root.querySelectorAll(".ag-row").forEach(b => b.onclick = () => renderProfile(b.dataset.agent));
}

// Home is the tab you come back to most, so it paints from the cached brief immediately
// and refreshes behind it. The refresh only repaints if the brief actually changed AND
// you are still on the screen it was painted for — SCREEN is bumped by every paint, so a
// slow response that lands after you've navigated away is dropped instead of clobbering
// whatever you opened.
async function renderHome() {
  const cached = S.igHome;
  if (!cached) {
    busy();
    drawHome(await loadHome());
    return;
  }
  drawHome(cached);
  const token = SCREEN;
  const fresh = await loadHome(true);
  if (SCREEN === token && JSON.stringify(fresh) !== JSON.stringify(cached)) drawHome(fresh);
}

// Frame 14's footer strip. TWO figures, not the frame's three — "about 2h 10m of busywork
// handled" has no source, and nothing measures run duration. Hidden entirely in a week
// with nothing in it, because "0 runs · 0 actions approved" is a worse thing to read
// under your morning brief than no line at all.
function weekLine(week) {
  const runs = (week && week.runs) || 0, ok = (week && week.approved) || 0;
  if (!runs && !ok) return "";
  const bits = [`${runs} run${runs === 1 ? "" : "s"}`];
  if (ok) bits.push(`${ok} action${ok === 1 ? "" : "s"} approved`);
  return `<div class="wk-line">This week: ${bits.join(" · ")}</div>`;
}

function drawHome(home) {
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
      <span class="ov-sub">${esc(bits.join(" · "))}${last && last.ran_at ? " · " + esc(threadStamp(last.ran_at)) : ""}</span>
    </button>`;
  };

  tb.paint(`
    <div class="brief">
      <div class="brief-stamp">${esc(stamp)}</div>
      <h1 class="brief-head">${headline}</h1>
      <p class="brief-lead">${lead}</p>
      <div class="row" style="gap:10px;">
        ${total ? `<button id="hmReview" class="btn-primary" style="padding:13px 26px;">Start review · ${total}</button>` : ""}
        <button id="hmAll" class="btn-secondary" style="padding:13px 22px;">See all runs</button>
      </div>
      ${agents.length ? `<div class="ov-wrap">
        <div class="label">Overnight</div>
        <div class="ov-row">${need.concat(clean).map(card).join("")}</div>
      </div>` : `<p class="muted">No agents yet — record a workflow to build your first.</p>`}
      ${weekLine(home.week)}
    </div>`);
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
            ${avatar(a.name, 34, a)}
            <span><b>${esc(a.name)}</b><span class="muted" style="margin-left:8px;font-size:12.5px;">${esc(ranWhen(st.ran_at))}</span></span>
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
              ${pending.length ? `<button class="btn-primary approve-sel" style="margin-top:12px;">Approve &amp; run selected</button>` : ""}
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
      const id = b.dataset.id;
      // Settle the card first: buttons out, outcome in, the instant you click. The send
      // takes seconds and the refresh longer, and a live Approve & send under something
      // you already approved invites a second click on the same email.
      settleProposalCard(id, "Sending…", true);
      const r = await api("POST", `/api/u/agents/${agentId}/approve-actions`,
                          { pending_ids: [id] });
      settleProposalCard(id, r.ok ? "✓ Done — this was sent"
                                  : "Couldn't send — try again from the agent's page", r.ok);
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
      const reason = await askWhy();                 // same prompt as Session history
      if (reason === null) return;
      await api("POST", `/api/u/pending/${b.dataset.id}/reject`, { reason });
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
  busy();
  const d = S.igHome || await loadHome();
  const tb = agentsTopbar(d.unread_total, null, d.needs_you_total, "messages");
  tb.paint(`
    <div class="panel">
      <h2>Messages</h2>
      <p class="lead">A direct line to each of your agents — ask what it's been doing, or tell it something it should know.</p>
      <div class="stack" id="dmList"><p class="muted">Loading…</p></div>
    </div>`);
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
      <span class="sub muted">${last ? esc(threadStamp(last.created_at)) : ""}</span>
      ${a.unread ? `<span class="flag-dot" title="${a.unread} unread"></span>` : ""}
    </button>`).join("");
  const el = $("#dmList");
  if (el) el.innerHTML = list || `<p class="muted">No agents yet.</p>`;
  root.querySelectorAll(".ag-row").forEach(b => b.onclick = () => renderThread(b.dataset.agent));
}

async function renderThread(agentId) {
  clearTimers();
  busy();
  await api("POST", `/api/u/agents/${agentId}/messages/read`, {});
  const home = await loadHome();
  const me = (home.agents || []).find(x => x.agent_id === agentId) || { name: "Agent" };
  const tb = agentsTopbar(home.unread_total, renderMessages, home.needs_you_total, "messages");
  // Frame 23: the thread says what it can actually do — answers come from its run
  // history, not from a fresh session — and offers openers, so the box isn't a blank
  // prompt you have to invent a question for.
  const SUGGESTIONS = ["What did you do today?", "Anything you're stuck on?",
                       "What did you skip and why?"];
  tb.paint(`
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
    </div>`);
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
    if (head) head.innerHTML = `${avatar(name, 26, me)}<b style="font-size:14px;">${esc(name)}</b>`;
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

/* ---------------- destructive confirm (28) ---------------- */

// Frame 28. Replaces window.confirm() for anything that destroys data: a native dialog
// can be dismissed with a stray Enter, and it can't say what is about to be lost. This
// one keeps "Delete forever" disabled until the word is typed exactly, so agreeing has
// to be deliberate. Resolves true only on confirm — Escape, Cancel and a scrim click
// all resolve false, so a caller can simply `if (!await confirmDestructive(…)) return`.
// Reuses .pop-back/.pop so every modal in the app is the same object.
// `word: null` drops the typed confirmation — for choices that are consequential but not
// destructive (turning off every approval gate, retiring a lesson). Those were still
// window.confirm(), which in a redesigned app looks like the browser interrupting it.
function confirmDestructive({ title, body, verb = "Delete forever", word = "delete",
                              tone = "danger" }) {
  return new Promise(resolve => {
    const back = document.createElement("div");
    back.className = "pop-back";
    back.innerHTML = `<div class="pop pop-confirm" role="alertdialog" aria-modal="true"
                           aria-labelledby="cfTitle">
      <div class="pop-title" id="cfTitle">${esc(title)}</div>
      <p class="cf-body">${esc(body)}</p>
      ${word ? `<input type="text" id="cfWord" class="cf-input" autocomplete="off"
             aria-label="Type ${esc(word)} to confirm" placeholder="Type ${esc(word)} to confirm">` : ""}
      <div class="cf-acts">
        <button class="btn-secondary" id="cfNo">Cancel</button>
        <button class="${tone === "danger" ? "btn-destroy" : "btn-primary"}" id="cfYes"
                ${word ? "disabled" : ""}>${esc(verb)}</button>
      </div>
    </div>`;
    document.body.appendChild(back);

    const input = back.querySelector("#cfWord");
    const yes = back.querySelector("#cfYes");
    const done = (v) => {
      document.removeEventListener("keydown", onKey);
      back.remove();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === "Escape") done(false);
      if (e.key === "Enter" && !yes.disabled) done(true);
    };
    if (input) {
      input.oninput = () => {
        yes.disabled = input.value.trim().toLowerCase() !== word;
      };
    }
    back.querySelector("#cfNo").onclick = () => done(false);
    yes.onclick = () => done(true);
    back.onclick = (e) => { if (e.target === back) done(false); };   // scrim, not the card
    document.addEventListener("keydown", onKey);
    (input || yes).focus();
  });
}

// Frame 21's Overview reads the schedule as a sentence — "Every weekday · 6:00 AM" —
// rather than only as a dropdown value.
function cadenceLabel(sched) {
  const v = cadenceToValue(sched && sched.cadence);
  const hit = CADENCES.find(c => c.v === v);
  return hit ? (hit.v ? "Runs " + hit.label : "Runs when you say so") : "Runs when you say so";
}

// "Next run tomorrow, 9:00 AM". The frame also says "takes ~9 min"; nothing measures run
// duration, so that half is left off rather than guessed.
function nextRunLine(sched) {
  const v = cadenceToValue(sched && sched.cadence);
  if (!v) return "Runs only when you ask it to.";
  const m = /(\d{1,2})\s*(am|pm)/i.exec(String(sched && sched.cadence) || "");
  const when = m ? ` at ${m[1]}${m[2].toLowerCase()}` : "";
  return v === "daily" ? `Next run tomorrow${when}.`
       : v === "weekly" ? `Next run next week${when}.`
       : `Next run on its schedule${when}.`;
}

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

/* ---------------- go-live colour assignment (29) ---------------- */

// The 25 shades, core five first so early agents get the strong colours — the same
// order the popover offers them in.
const ALL_SHADES = CORE_AGENT_COLORS.concat(EXTENDED_SHADES);

// Hue in degrees, for measuring how far apart two shades read.
function hexHue(hex) {
  const s = String(hex).replace("#", "");
  const r = parseInt(s.slice(0, 2), 16) / 255;
  const g = parseInt(s.slice(2, 4), 16) / 255;
  const b = parseInt(s.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  if (!d) return 0;
  let h;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  return (h * 60 + 360) % 360;
}

// Frame 29: the most distinct unused shade — the one whose nearest neighbour among the
// colours already in use is furthest away. No shade repeats until all 25 are spoken for;
// after that it cycles, and the popover already marks a taken shade with its owner.
function pickShade(taken) {
  const used = new Set((taken || []).filter(Boolean).map(c => String(c).toUpperCase()));
  // The core five are spent FIRST — frame 29's whole premise is the sixth agent, the one
  // that arrives after "all 5 core colours are taken". Picking on hue distance alone
  // reaches into the extended tones from the second agent onward, which gets you a muted
  // palette before you have five agents.
  const freeCore = CORE_AGENT_COLORS.filter(c => !used.has(c.toUpperCase()));
  const free = freeCore.length ? freeCore : ALL_SHADES.filter(c => !used.has(c.toUpperCase()));
  if (!free.length) return ALL_SHADES[used.size % ALL_SHADES.length];
  if (!used.size) return free[0];
  const hues = Array.from(used).map(hexHue);
  let best = free[0], bestGap = -1;
  for (const c of free) {
    const h = hexHue(c);
    const gap = Math.min(...hues.map(t => {
      const d = Math.abs(h - t) % 360;
      return Math.min(d, 360 - d);
    }));
    if (gap > bestGap) { bestGap = gap; best = c; }
  }
  return best;
}

// Give every agent that just went live its own shade. Runs AFTER the deploy has already
// succeeded and is entirely best-effort: the colour endpoint is the same one the popover
// uses, and if any of it fails the agent simply keeps the derived default. Nothing here
// may affect whether the deploy is considered to have worked.
async function assignShadesTo(newIds) {
  if (!newIds || !newIds.length) return;
  try {
    const home = await loadHome(true);
    const agents = home.agents || [];
    const fresh = new Set(newIds);
    const taken = agents.filter(a => !fresh.has(a.agent_id)).map(a => a.color);
    for (const id of newIds) {
      const shade = pickShade(taken);
      const r = await api("POST", `/api/u/agents/${id}/color`, { color: shade });
      if (!r.ok) return;                       // endpoint unhappy — leave the rest alone
      taken.push(shade);                       // so the next one picks against this too
    }
    S.igHome = null;                           // colours changed; next read must be fresh
  } catch (e) { /* a colour is never worth failing a go-live over */ }
}

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
        <button class="btn-primary" id="popSave" style="flex:1;">Save</button>
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



/* Frame 21's subtitle is one readable sentence — "Checks retailer portals for price +
   casepack drift · live since May 12". The old version welded fragments together and
   an agent with no description got "Runs when: on a new matching task · live since
   Aug 12": a sentence stem, a trigger phrase and a date, none of which agree.

   A description is a sentence, so it stands alone. A trigger is a clause, so it needs
   framing — and a vague one ("on a new matching task") says less than nothing, so it
   is dropped rather than dressed up. Parts that are absent leave no separator behind. */
const VAGUE_TRIGGERS = ["on a new matching task", "when you say so", "manual", ""];

function profileBio(a) {
  const bits = [];
  const desc = (a.description || "").trim();
  const trigger = (a.trigger || "").trim();
  if (desc) {
    bits.push(esc(desc));
  } else if (trigger && !VAGUE_TRIGGERS.includes(trigger.toLowerCase())) {
    bits.push(`Runs ${esc(trigger)}`);
  } else {
    bits.push("Runs when you ask it to");
  }
  if (a.deployed_at) bits.push(`live since ${esc(onDate(a.deployed_at))}`);
  if (a.shared_from) bits.push(`${esc(a.shared_from)}'s agent`);
  return bits.join(" · ");
}

async function renderProfile(agentId) {
  clearTimers();
  busy();
  const home = S.igHome || await loadHome();
  const a = (home.agents || []).find(x => x.agent_id === agentId) || {};
  const name = a.name || agentId;
  const sched = a.schedule || null;
  const cur = cadenceToValue(sched && sched.cadence);
  const paused = sched && sched.paused;
  const opts = CADENCES.map(c => `<option value="${c.v}" ${c.v === cur ? "selected" : ""}>${c.label}</option>`).join("");
  const tb = agentsTopbar(home.unread_total, null, home.needs_you_total, "home");
  tb.paint(`
    <div class="panel">
      <div class="row" style="gap:16px;align-items:center;">
        ${avatar(name, 64, a)}
        <div style="flex:1;min-width:0;">
          <div class="row" style="gap:9px;"><h2 id="pTitle" style="margin:0;">${esc(name)}</h2>
            <button id="pEdit" class="pencil" title="Rename or recolor">✎</button>
            <span class="pill ${paused ? "pill-warn" : "pill-on"}">${paused ? "PAUSED" : "LIVE"}</span></div>
          <p class="ag-bio wrap" style="margin:5px 0 0;">${profileBio(a)}</p>
        </div>
        <div class="row" style="gap:9px;flex:none;">
          ${a.can_edit === false ? `
          <span class="pill pill-off">SHARED WITH YOU</span>
          <button class="btn-primary btn-sm" id="pTemplate">Build my own</button>` : `
          <button class="btn-secondary btn-sm" id="pMsg">Message</button>
          <button class="btn-primary btn-sm" id="pRun">Run now</button>
          ${sched ? `<button class="btn-secondary btn-sm" id="pPause">${
            paused ? "Resume" : "⏸ Pause"}</button>` : ""}`}
        </div>
      </div>
      <div class="tabs" id="pTabs">
        ${["Overview", "Rules", "Lessons", "History"].map((t, n) =>
          `<button class="tab${n === 0 ? " on" : ""}" data-pane="${t.toLowerCase()}">${t}</button>`).join("")}
      </div>
    </div>

    <!-- Frame 21's Overview: Schedule, Permissions and the run stats as their own
         cards. The schedule select used to sit loose in the header row beside Message
         and Run now, which put a settings control among the actions and left Overview
         with nothing of its own. -->
    <div class="panel pane-bare" data-pane="overview">
      <div class="prof-grid">
        <div class="prof-main">
          <div class="prof-card">
            <div class="label">Schedule</div>
            <div class="row" style="gap:10px;align-items:center;flex-wrap:wrap;">
              <span class="ov-strong">${esc(cadenceLabel(sched))}</span>
              ${a.can_edit === false ? "" : `<select class="sched" id="pSched">${opts}</select>`}
            </div>
            <div class="sub muted" id="pNext">${paused
              ? "Paused — it won't run until you resume it."
              : esc(nextRunLine(sched))}</div>
          </div>
          <div class="prof-card" id="pPerms">
            <div class="label">Permissions</div>
            <p class="sub muted">Loading…</p>
          </div>
          <div class="prof-stats" id="pStatRow"></div>
          ${a.capture_present ? `
          <div class="prof-card prof-danger">
            <div style="flex:1;">
              <div class="t">Delete this agent's capture data</div>
              <div class="d">Keeps the agent; removes its recordings. This can't be undone.</div>
            </div>
            <button id="pDelCap" class="btn-danger btn-sm">Delete…</button>
          </div>` : `
          <div class="prof-card"><div class="sub muted">Capture data deleted.</div></div>`}
        </div>
        <div class="prof-rail">
          ${(a.lesson_confirmations || []).map(c => `
          <div class="prof-card rail-confirm">
            <div class="label">Shared lesson — confirm?</div>
            <div class="body">${esc(c.lesson || "")}</div>
            <div class="row" style="gap:8px;">
              <button class="btn-primary btn-sm lesson-yes" data-agent="${esc(agentId)}"
                data-id="${esc(c.decision_id)}">Adopt</button>
              <button class="btn-secondary btn-sm lesson-no" data-agent="${esc(agentId)}"
                data-id="${esc(c.decision_id)}">Ignore</button>
            </div>
          </div>`).join("")}
          ${(a.active_lessons && a.active_lessons.length) ? `
          <div class="prof-card">
            <div class="label">Active lessons · ${a.active_lessons.length}</div>
            ${a.active_lessons.map(c => `
            <div class="rail-lesson">
              <span class="rail-dot"></span>
              <div class="txt">${esc(c.lesson || "")}</div>
              <button class="btn-link lesson-retire" data-agent="${esc(agentId)}"
                data-id="${esc(c.decision_id)}" style="font-size:11px;">Retire</button>
            </div>`).join("")}
          </div>` : ""}
          <div class="prof-card" id="pRail">
            <div class="label">Recent runs</div>
            <p class="sub muted">Loading…</p>
          </div>
        </div>
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
    <!-- Rules and History were both EMPTY: every panel below the header was untagged, so
         showPane treated them all as Overview and those two tabs displayed nothing at
         all. Tagging them is the fix; the escalation block below is what Rules is for. -->
    <div class="panel" data-pane="rules">
      <h2>When it stops and asks you</h2>
      <p class="sub muted" style="margin-top:2px;">Set for every agent when you built
        them. Anything outbound is always staged — that part can't be switched off.</p>
      <div id="pRules" style="margin-top:10px;"><p class="muted">Loading…</p></div>
    </div>
    <div class="panel" data-pane="rules" id="pApprovalsPanel" style="display:none;">
      <h2>Approvals</h2>
      <p class="sub muted" style="margin-top:2px;">Choose which apps need your approval before the agent acts. Others run automatically. Takes effect on the next run.</p>
      <div id="pApprovals" style="margin-top:10px;"></div>
    </div>
    <div class="panel" data-pane="rules" id="pSharingPanel" style="display:none;">
      <h2>Lesson sharing (this agent)</h2>
      <p class="sub muted" style="margin-top:2px;">Override your account default for just this agent, or leave it to inherit.</p>
      <div id="pSharing" style="margin-top:10px;"></div>
    </div>
    <div class="panel" data-pane="history">
      <div class="spread"><h2>Session history</h2>
        ${a.unreviewed ? `<button class="btn-secondary btn-sm" id="pReview">Review ${a.unreviewed} waiting →</button>` : ""}</div>
      <div id="pLog"><p class="muted">Loading…</p></div>
    </div>`);
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
      // `pane-empty` is how a panel says it has nothing to show — Approvals with no
      // connected apps, for instance. Without this, showPane's blanket display write
      // overrode the conditional reveal and those panels appeared empty regardless.
      el.style.display = (pane === want && !el.classList.contains("pane-empty"))
        ? "" : "none";
    });
    root.querySelectorAll("#pTabs .tab").forEach(t =>
      t.classList.toggle("on", t.dataset.pane === want));
  };
  root.querySelectorAll("#pTabs .tab").forEach(t =>
    t.onclick = () => showPane(t.dataset.pane));
  showPane("overview");

  // A shared agent has no run/pause/schedule/delete controls, so the handlers below
  // simply find nothing to wire — every one is already guarded by an if. Returning
  // early here instead would ALSO skip the async fills further down, and the
  // Permissions, Rules and Recent-runs cards would sit on "Loading…" forever. The
  // point of team visibility is seeing those, so the fills must run.
  // "Build my own" on a teammate's agent: their workflow description seeds a session
  // owned by ME, and the ordinary wizard takes it from there against my accounts. No
  // agent changes hands and no recording is copied.
  const tpl = $("#pTemplate");
  if (tpl) tpl.onclick = async () => {
    tpl.disabled = true; tpl.textContent = "Setting up…";
    const { ok, data } = await api("POST", `/api/u/agents/${agentId}/use-as-template`, {});
    if (!ok) {
      // The profile has no message row of its own, and the button is the thing the
      // user is looking at — so it carries the failure rather than a bare alert().
      tpl.disabled = false;
      tpl.textContent = friendly((data || {}).error || "could not start");
      return;
    }
    S.igHome = null;
    await loadAndRender();
  };

  const dc = $("#pDelCap");
  if (dc) dc.onclick = async () => {
    const okd = await confirmDestructive({
      title: `Delete ${a.name || "this agent"}'s capture data?`,
      body: "Removes its recordings from your device and the cloud, permanently. The agent keeps working — it just can't be retrained from this data. This can't be undone.",
    });
    if (!okd) return;
    dc.disabled = true; dc.textContent = "Deleting…";
    await api("POST", `/api/u/agents/${agentId}/delete-capture-data`, {});
    S.igHome = null; renderProfile(agentId);
  };
  // the log: real per-session activity (summaries + proposals), newest first
  const { data } = await api("GET", `/api/u/agents/${agentId}/activity`);
  const sessionsList = (data && data.activity) || [];
  // Approvals: per-app staged/autonomous toggles (post-deploy, no redeploy). All
  // interpolated values are escaped via esc() (the file's XSS-safe rendering idiom).
  // Rules (21): the escalation rules from frame 10, read back. Stored as one sentence
  // per agent — "Stop and ask me when any of these is true:" then a line per rule — so
  // it renders as the list it was written as rather than as a paragraph.
  const rulesBox = $("#pRules");
  if (rulesBox) {
    const raw = (data && data.agent && data.agent.rules) || "";
    const lines = raw.split("\n").map(s => s.trim())
      .filter(s => s.startsWith("-")).map(s => s.replace(/^-\s*/, ""));
    rulesBox.innerHTML = lines.length
      ? `<div class="sn-list">${lines.map(l => {
          const [head, ...rest] = l.split(":");
          const tail = rest.join(":").trim();
          return `<div class="sn-row">
            <div class="sn-mid"><div class="sn-title">${esc(head.trim())}</div>
              ${tail ? `<div class="sn-sub">${esc(tail)}</div>` : ""}</div>
            ${/outbound/i.test(head) ? `<span class="sn-built">BUILT IN</span>` : ""}
          </div>`;
        }).join("")}</div>
        <p class="sub muted" style="margin-top:10px;">Changing these per agent isn't built
          yet — they come from what you set when the agents were created.</p>`
      : `<p class="muted">No rules recorded for this agent. Agents built before the safety
          step still stage everything outbound — that part is always on.</p>`;
  }
  const availApps = (data && data.agent && data.agent.available_apps) || [];
  const apPanel = $("#pApprovalsPanel");
  if (apPanel && !availApps.length) apPanel.classList.add("pane-empty");
  if (apPanel && availApps.length) {
    apPanel.classList.remove("pane-empty");
    const apMarkup = availApps.map(ap => `
      <label class="row" style="gap:8px;margin-top:6px;cursor:pointer;">
        <input type="checkbox" class="stage-toggle" data-slug="${esc(ap.slug)}" ${ap.staged ? "checked" : ""}>
        <span class="title" style="font-size:14px;">${esc(ap.label)}</span>
        <span class="sub muted">${ap.staged ? "needs approval" : "runs automatically"}</span>
      </label>`).join("")
      + `<button class="btn-primary btn-sm" id="pStageSave" style="margin-top:12px;">Save approval settings</button>`;
    $("#pApprovals").insertAdjacentHTML("beforeend", apMarkup);
    $("#pStageSave").onclick = async (e) => {
      const staged = Array.from(root.querySelectorAll(".stage-toggle"))
        .filter(c => c.checked).map(c => c.dataset.slug);
      if (!staged.length && !await confirmDestructive({
        title: "Let this agent act without review?",
        body: "No app will need your approval — everything it does, including anything outbound, happens without showing you first.",
        verb: "Turn off approvals", word: null, tone: "primary",
      })) return;
      e.target.disabled = true; e.target.textContent = "Saving…";
      await api("POST", `/api/u/agents/${agentId}/staging`, { staged_apps: staged });
      S.igHome = null; renderProfile(agentId);
    };
  }
  // Per-agent lesson-sharing override (Phase B). Empty override = inherit account default.
  const consent = data && data.agent && data.agent.consent;
  const shPanel = $("#pSharingPanel");
  if (shPanel && !consent) shPanel.classList.add("pane-empty");
  if (shPanel && consent) {
    shPanel.classList.remove("pane-empty");
    const ov = consent.override || {};
    $("#pSharing").insertAdjacentHTML("beforeend", `
      <div class="row" style="gap:10px;align-items:center;">
        <span class="label" style="min-width:170px;">Contribute my lessons</span>${scopeSelect("ag-contrib", ov.contribute_scope, "contribute", true)}</div>
      <div class="row" style="gap:10px;margin-top:8px;align-items:center;">
        <span class="label" style="min-width:170px;">Use others' lessons</span>${scopeSelect("ag-consume", ov.consume_scope, "consume", true)}</div>
      <button class="btn-primary btn-sm" id="pShareSave" style="margin-top:12px;">Save</button>`);
    $("#pShareSave").onclick = async (e) => {
      await api("POST", `/api/u/agents/${agentId}/consent`, {
        contribute_scope: $(".ag-contrib").value || "inherit",
        consume_scope: $(".ag-consume").value || "inherit" });
      e.target.textContent = "Saved ✓";
    };
  }
  const statRow = $("#pStatRow");
  if (statRow) {
    const props = sessionsList.flatMap(s => s.proposals || []);
    const decided = props.filter(p =>
      p.status && p.status !== "pending" && p.status !== "queued"
      && p.status !== "executing");
    const asIs = decided.filter(p => p.status === "executed" || p.status === "approved");
    const pct = decided.length ? Math.round((asIs.length / decided.length) * 100) : null;
    // Frame 21 shows three figures as cards. The third is "saved", which nothing
    // measures — hours a human DIDN'T spend is not observable from a run log, and
    // guessing it would put an invented number on the one screen a customer uses to
    // judge whether the agent is worth keeping. agent_runs does carry started_at and
    // ended_at, so typical run length is real, and it answers the question the schedule
    // card actually needs ("takes ~9 min" in the frame).
    const mins = runMinutes(sessionsList);
    statRow.innerHTML = `
      <div class="prof-stat"><b>${sessionsList.length}</b><span>run${
        sessionsList.length === 1 ? "" : "s"}</span></div>
      ${pct === null ? "" : `<div class="prof-stat"><b>${pct}%</b><span>approved as-is</span></div>`}
      ${mins === null ? "" : `<div class="prof-stat"><b>${mins}m</b><span>typical run</span></div>`}`;
  }
  // "Next run tomorrow 6:00 AM · takes ~9 min" — the frame puts expected duration on the
  // schedule line, which is where it is actually useful: it tells you what window the
  // agent occupies, not merely when it starts. Appended rather than rendered inline
  // because it needs the run log, which arrives after first paint.
  const nextLine = $("#pNext");
  if (nextLine && mins !== null && !paused) {
    nextLine.textContent = `${nextLine.textContent} · takes ~${mins} min`;
  }
  // Recent runs — the rail's third card in frame 21.
  const rail = $("#pRail");
  if (rail) {
    const recent = sessionsList.slice(0, 4);
    rail.innerHTML = `<div class="label">Recent runs</div>` + (recent.length
      ? recent.map(r => {
          const stopped = (r.decisions || []).length > 0;
          const held = (r.proposals || []).length;
          const bits = [];
          if (held) bits.push(`${held} awaiting approval`);
          if (r.note) bits.push(r.note);
          return `<div class="rail-run">
            <div class="hd"><b>${esc(whenShort(r.ran_at))}</b>
              <span class="st ${stopped ? "stop" : "ok"}">${
                stopped ? "stopped — needs you" : esc(r.status || "clean")}</span></div>
            ${bits.length ? `<div class="dt">${esc(bits.join(" · "))}</div>` : ""}
          </div>`;
        }).join("")
      : `<p class="sub muted">No runs yet.</p>`);
  }
  // Permissions (21): which apps it can touch, and whether each is held for approval.
  const perms = $("#pPerms");
  if (perms) {
    perms.innerHTML = `<div class="label">Permissions</div>` + (availApps.length
      ? `<div class="ov-perms">${availApps.map(ap => `
          <div class="ov-perm"><span class="ov-perm-n">${esc(ap.label || ap.slug)}</span>
            <span class="pill ${ap.staged ? "pill-warn" : "pill-on"}">${
              ap.staged ? "ALWAYS STAGED" : "RUNS AUTOMATICALLY"}</span></div>`).join("")}</div>
         <div class="sub muted" style="margin-top:8px;">Staged means it prepares the
           action and waits for you. Change these under Rules.</div>
         ${escalationLine(data)}`
      : `<p class="sub muted" style="margin-top:4px;">No connected apps yet.</p>`);
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
        ${approvable.length ? `<button class="btn-primary btn-sm approve-sel" style="margin-top:10px;">Approve &amp; run selected</button>` : ""}
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
  // Approve & send / Edit first / Reject in Session history. These were never wired
  // here — only in the story overlay — so all three did nothing on this screen.
  wireProposalActions(root, agentId, () =>
    S._timers.push(setTimeout(() => { S.igHome = null; renderProfile(agentId); }, 4000)));
  root.querySelectorAll(".prop-edit").forEach(b => b.onclick = () => {
    // Same as the overlay: opens the payload. Editing the content itself is still to
    // build — the button at least does the visible half now instead of nothing.
    const box = root.querySelector(`.pp-payload[data-for="${b.dataset.id}"]`);
    if (box) box.hidden = false;
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
  root.querySelectorAll(".lesson-retire").forEach(b => b.onclick = async () => {
    const ok = await confirmDestructive({
      title: "Retire this lesson?",
      body: "The agent stops applying it from its next run. What it already did stays as it is.",
      verb: "Retire it", word: null, tone: "primary",
    });
    if (ok) confirmLesson(b.dataset.agent, b.dataset.id, "retire");
  });
}
const renderActivity = renderProfile;                        // legacy entry point

/* ---------------- 01 RECORD ---------------- */
function stepRecord() {
  // Frame 06 is one tall centered stage, not a titled panel: while you are recording the
  // timer IS the screen, and an eyebrow + heading above it just competes with the thing
  // you are actually watching. The stage holds all three modes so the step doesn't change
  // shape underneath you when you start.
  shell(`<div class="rec-stage" id="recBody"></div>`);
  renderRecBody("idle");
  // If a capture is already running — you navigated away and came back — show the
  // recording view rather than an idle screen whose Start button answers
  // already_running with no route to Stop. The server owns the elapsed time, so the
  // clock resumes where the capture actually is instead of at zero.
  api("GET", "/api/u/capture/status").then(({ ok, data }) => {
    if (!ok || !data || !data.running || !$("#recBody")) return;
    const started = data.elapsed_seconds
      ? Date.now() - Math.round(data.elapsed_seconds * 1000) : Date.now();
    renderRecBody("recording", started, data.paused);
  }).catch(() => { /* idle is the safe default */ });
}

// Frame 06's waveform: fixed heights rather than a formula, because the drawn rhythm is
// deliberately irregular and `10 + (i % 4) * 8` reads as a sawtooth.
const REC_BARS = [14, 26, 18, 34, 22, 38, 16, 28, 12];

// `wasPaused` is only passed when resuming a capture that was already running — the
// screen has to come back in the state the capture is actually in, not in the state it
// was in when you last looked at it.
function renderRecBody(mode, started, wasPaused) {
  const b = $("#recBody");
  if (mode === "recording") {
    b.innerHTML = `
      <div class="rec-mid">
        <div class="rec-live" id="recLive"><span class="rec-dot"></span>RECORDING</div>
        <div class="big-timer" id="recTime">0:00:00</div>
        <p class="rec-copy">Work like normal. Palza is quietly noting what repeats — you
          review everything it thinks it found before anything is built.</p>
        <div class="waveform" id="recWave">${REC_BARS.map(h =>
          `<b style="height:${h}px;"></b>`).join("")}</div>
        <p id="recMsg" class="msg err"></p>
      </div>
      <div class="rec-acts">
        <button id="recPause" class="btn-secondary">Pause</button>
        <button id="recStop" class="btn-accent">Finish recording</button>
      </div>`;
    // Elapsed is tracked as an origin that slides forward while paused, so the clock
    // reports time actually recorded rather than wall-clock since you pressed start.
    let t0 = started || Date.now();
    let paused = !!wasPaused, pausedAt = paused ? Date.now() : 0;
    if (paused) {                                // resumed onto a paused capture
      const btn = $("#recPause"); if (btn) btn.textContent = "Resume";
      const live = $("#recLive");
      if (live) { live.classList.add("off"); live.lastChild.textContent = "PAUSED"; }
      const wv = $("#recWave"); if (wv) wv.classList.add("still");
    }
    const tick = () => {
      const el = $("#recTime");
      if (!el) return;
      const s = Math.floor((Date.now() - t0) / 1000);
      const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
      const ss = String(s % 60).padStart(2, "0");
      el.textContent = `${Math.floor(s / 3600)}:${mm}:${ss}`;   // 2:14:36, per the frame
      if (!paused) S._timers.push(setTimeout(tick, 1000));
    };
    tick();
    $("#recPause").onclick = async () => {
      const btn = $("#recPause");
      btn.disabled = true;
      const goingDown = !paused;
      const { ok, data } = await api("POST", `/api/u/capture/${goingDown ? "pause" : "resume"}`, {});
      btn.disabled = false;
      if (!ok) {
        // Usually "not_running" — the capture ended underneath us. Say so rather than
        // letting the button look broken, and keep the clock where it was.
        $("#recMsg").textContent = data.error === "not_running"
          ? "the recording already stopped — finish up to save what you have"
          : friendly(data.error);
        return;
      }
      $("#recMsg").textContent = "";
      paused = goingDown;
      if (paused) {
        pausedAt = Date.now();
        clearTimers();
      } else {
        t0 += Date.now() - pausedAt;         // the gap never counts as recorded time
        tick();
      }
      btn.textContent = paused ? "Resume" : "Pause";
      const live = $("#recLive");
      live.classList.toggle("off", paused);
      live.lastChild.textContent = paused ? "PAUSED" : "RECORDING";
      $("#recWave").classList.toggle("still", paused);
    };
    $("#recStop").onclick = async () => {
      $("#recStop").disabled = true; $("#recStop").textContent = "Saving…";
      await api("POST", "/api/u/capture/stop", {});
      await loadAndRender();
    };
    return;
  }
  if (mode === "describe") {
    b.innerHTML = `
      <div class="rec-mid rec-mid-form">
        <h1 class="rec-head">Describe your workflow</h1>
        <textarea id="descText" rows="7" placeholder="In a few sentences: what kicks this work off, the steps you take, the apps you use — name them (e.g. Gmail, Google Sheets) — and what the finished result looks like."></textarea>
        <p class="notice">The more concretely you name your apps and steps, the better your agent will be. You'll review and correct what palza understood next.</p>
        <p id="recMsg" class="msg err"></p>
      </div>
      <div class="rec-acts">
        <button id="descBack" class="btn-secondary">← Back</button>
        <button id="descGo" class="btn-primary">Continue</button>
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
    <div class="rec-mid">
      <h1 class="rec-head">Record your day</h1>
      <p class="rec-copy">Palza watches how you work for a while and learns your routines.
        On your Mac · no screenshots · reads the fields and buttons you use ·
        sensitive text removed before saving.</p>
      <div id="permStrip"></div>
      <p id="recMsg" class="msg err"></p>
    </div>
    <div class="rec-acts rec-acts-col">
      <div class="rec-acts">
        <button id="recSample" class="btn-secondary">Use a sample day</button>
        <button id="recStart" class="btn-accent">Start recording</button>
      </div>
      <button id="recDescribe" class="btn-link">Skip recording — describe it instead</button>
    </div>`;
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
  paintPermissions();
}

/* Capture pre-flight. /api/permissions has existed since the first desktop build and
   nothing has ever called it, so the one failure that makes a whole session worthless
   has been invisible at the only moment it is still cheap to fix — before recording.
   Runs after paint (never blocking it) and says nothing when there is nothing wrong.

   A permission we cannot detect (granted === null) is deliberately silent: a check we
   could not run is not evidence of a problem, and nagging on it would train people to
   ignore the strip that matters. */
async function paintPermissions() {
  const host = $("#permStrip");
  if (!host) return;
  const { ok, data } = await api("GET", "/api/u/permissions");
  if (!ok || !data || data.platform !== "darwin") return;   // capture is macOS-only
  const denied = ["accessibility", "input_monitoring", "screen_recording"]
    .map((k) => data[k]).filter((c) => c && c.granted === false);
  if (!denied.length) { host.innerHTML = ""; return; }

  const blocking = denied.filter((c) => c.importance === "required");
  const names = (list) => list.map((c) => c.name).join(" and ");
  const lead = blocking.length
    ? `${names(blocking)} ${blocking.length > 1 ? "are" : "is"} off, so palza would see
       that you worked in an app but not which buttons you pressed or which fields you
       filled — not enough to rebuild a workflow.`
    : `${names(denied)} is off. Palza reads window titles another way, so recording still
       works; grant it only if titles come back blank.`;
  const needsAX = data.accessibility && data.accessibility.granted === false;
  host.innerHTML = `<p class="notice">${esc(lead.replace(/\s+/g, " "))}${needsAX
    ? ' <button type="button" class="fr-link" id="permGrant">Grant Accessibility</button>'
    : ""}</p>`;

  const grant = $("#permGrant");
  if (grant) grant.onclick = async () => {
    grant.disabled = true;
    grant.textContent = "Approve it in System Settings, then come back…";
    await api("POST", "/api/u/permissions/request", {});
    paintPermissions();                       // re-read rather than assume it took
  };
}

/* ---------------- 02 FIND WORKFLOWS (reconstruct + discover) ---------------- */
function stepWorkflows() {
  const sid = S.session.id, a = S.session.artifacts || {};
  // Frame 07. Every id runWorkflows touches (#wfBar, #wfMsg, #wfErr) is kept, so this is
  // presentation only — the reconstruct/discover flow underneath is untouched.
  //
  // The frame streams workflow cards in as they're found. Discovery is atomic here: the
  // job reports {state, current, total} and results exist only once it finishes, so there
  // is nothing truthful to stream. The frame's own dashed "still looking…" card is
  // exactly the right stand-in, and the progress bar stays — it carries real progress
  // that a looping animation cannot.
  shell(`
    <div class="rec-stage">
      <div class="wf-head">
        ${searchMark()}
        <h1 class="rec-head" id="wfMsg">${a.event_log ? "Looking for repeatable workflows…" : "Reading your day…"}</h1>
        <div class="wf-sub">${esc(capturedFor(S.session))}reading back the work that repeats</div>
        <div class="progress wf-bar"><i id="wfBar" style="width:0%"></i></div>
      </div>
      <div class="wf-list">
        <div class="wf-wait">still looking…</div>
      </div>
      <p id="wfErr" class="msg err"></p>
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
  // Frame 08's voice: it asks, rather than announcing what it produced.
  shell(`<div class="panel"><h1 class="rec-head" style="font-size:26px;">Did I get these right?</h1>
    <p class="lead">Correct anything — this is what your agents will be built from.
      Anything you mark <b>not a workflow</b> stays yours to handle.</p>
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

  // Frame 08 is ONE row of three, not two rows of two. It used to ask twice — first
  // "Looks right / Not quite", then "Automate / Manual" — which are the same decision
  // seen from two angles, and left you able to mark a workflow correct AND manual with
  // no idea which mattered. Three exclusive answers, and each writes both pieces of
  // state, so `understanding` and `classification` can no longer disagree.
  const REV_CHOICES = [["ok", "✓ Looks right"], ["fix", "Fix details"],
                       ["no", "Not a workflow"]];
  const choiceOf = (id) => {
    if (cls[id] === "anti_bundle" && und[id]) return "no";
    if (und[id] === "corrected") return "fix";
    if (und[id] === "confirmed") return "ok";
    return "";                                       // unanswered — Continue stays gated
  };
  const setChoice = (id, k) => {
    if (k === "ok") { und[id] = "confirmed"; cls[id] = "bundle"; }
    else if (k === "fix") { und[id] = "corrected"; cls[id] = "bundle"; }
    // "Not a workflow" IS the answer — there is nothing left to correct about a thing
    // you have just said is not a workflow, so it resolves rather than re-asking.
    else if (k === "no") { und[id] = "confirmed"; cls[id] = "anti_bundle"; }
  };
  // the one-line reason the engine gave (fall back to the internal "why" fields)
  const reasonOf = (it) => it.rationale || it.why_automatable || it.why_judgment || "";
  // plain-language summary of what palza thinks this workflow is (anti_bundles
  // have no trigger/steps; they carry why_judgment instead)
  const summary = (it) => {
    const rows = [];
    if (it.description) rows.push(`<div class="sub">${esc(it.description)}</div>`);
    // the trigger is the cadence line under the name now (08), so it isn't repeated here
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
        <div>
          <div class="title">${esc(it.name || id)}</div>
          ${it.trigger ? `<div class="rev-cadence">${esc(it.trigger)}</div>` : ""}
        </div>
        <span class="pill ${auto ? "pill-on" : "pill-off"}">${auto ? "AUTOMATE" : "MANUAL"}</span>
      </div>
      <div class="sub" style="margin-top:6px;">${reason ? esc(reason) : `<span class="muted">no reason given</span>`}</div>
      <div class="sub" style="margin-top:10px;"><b>What we think this workflow is</b></div>
      ${summary(it)}
      <div class="rev-acts">
        ${REV_CHOICES.map(([k, label]) =>
          `<button class="rev-3${choiceOf(id) === k ? " on" : ""}" data-id="${esc(id)}"
            data-choice="${k}">${label}</button>`).join("")}
      </div>
      ${choiceOf(id) === "fix" ? `<div class="field" style="margin-top:8px;">
        <textarea class="corr" data-id="${esc(id)}" rows="3" placeholder="In a line or two: what actually happens — what kicks it off, what you do, and where the result ends up.">${esc(corr[id])}</textarea>
      </div>` : ""}
    </div>`;
  };

  const gate = () => {
    const left = items.filter(it => !resolved(it.id)).length;
    const n = items.filter(it => cls[it.id] === "bundle").length;
    $("#revGo").disabled = !!left;
    $("#revGo").textContent = left
      ? `Answer ${left} more`                              // three choices now, not two
      : (n ? `Continue · ${n} confirmed` : "Continue");        // frame 08's wording
  };

  const paint = () => {
    $("#revStack").innerHTML = items.length
      ? items.map(card).join("") : `<p class="muted">Nothing to review.</p>`;
    root.querySelectorAll(".rev-3").forEach(b => b.onclick = () => {
      setChoice(b.dataset.id, b.dataset.choice); paint();
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
  // Rules already set on a previous pass, so coming back doesn't silently reset them.
  const priorRules = (() => {
    for (const bid of allIds) {
      const hit = ((interviews[bid] || {}).answers || [])
        .find(a => a.q === SAFETY_RULES_ID && String(a.answer || "").trim());
      if (hit) return hit.answer;
    }
    return "";
  })();
  const nameOf = (bid) => (data.names && data.names[bid]) || bid;

  // Frame 09's "Everyone" tab: a question several agents both need — tone, priority
  // contacts — asked ONCE instead of once per agent.
  //
  // Grouped here rather than tagged by the detector. A model deciding "this is a shared
  // question" can be wrong, and a wrong merge silently answers one agent's question with
  // another agent's answer. Normalised exact text is conservative: it only ever merges
  // questions that really are the same question, and a near-miss just stays per-agent,
  // which is the harmless direction to fail in.
  const qKey = (t) => String(t || "").toLowerCase()
    .replace(/[^a-z0-9]+/g, " ").trim();
  const shared = new Map();                            // key -> {question, suggested, owners}
  for (const bid of ids) {
    for (const q of (interviews[bid] || {}).questions || []) {
      const k = qKey(q.question);
      if (!k) continue;
      if (!shared.has(k)) {
        shared.set(k, { question: q.question, suggested: q.suggested_answers || [], owners: [] });
      }
      shared.get(k).owners.push({ bid, qid: q.id });
    }
  }
  for (const [k, v] of Array.from(shared)) {
    // Shared means MORE THAN ONE AGENT asks it. One owner is an ordinary question.
    if (new Set(v.owners.map(o => o.bid)).size < 2) shared.delete(k);
  }
  const sharedKeys = Array.from(shared.keys());
  const isShared = (bid, qid) => sharedKeys.some(k =>
    shared.get(k).owners.some(o => o.bid === bid && o.qid === qid));
  // A prior answer to any owner of a shared question pre-fills the single field.
  const sharedPrior = (k) => {
    for (const o of shared.get(k).owners) {
      const hit = ((interviews[o.bid] || {}).answers || []).find(a => a.q === o.qid);
      if (hit && String(hit.answer || "").trim()) return hit.answer;
    }
    return "";
  };

  let page = 0;
  // Declared HERE, not beside the paging helpers: pills() reads it and pills() is called
  // inside the innerHTML template below, so a later `const` would be a TDZ crash on the
  // whole interview step rather than a subtle bug.
  //
  // Page order: Everyone first when it exists, then the agents that still have questions
  // of their own. An agent whose every question was shared gets no page — it is already
  // fully answered from the Everyone tab.
  const pageIds = (sharedKeys.length ? ["__everyone"] : []).concat(
    ids.filter(bid => ((interviews[bid] || {}).questions || [])
      .some(q => !isShared(bid, q.id))));

  const everyoneBlock = sharedKeys.length ? `
    <div class="iv-page" data-bid="__everyone" style="display:none;">
      <div class="card" style="padding:16px;">
        <div class="label" style="margin-bottom:10px;">Everyone</div>
        <p class="sub muted" style="margin:-4px 0 12px;">${sharedKeys.length}
          question${sharedKeys.length === 1 ? "" : "s"} more than one agent needs. Answer
          once and every one of them gets it.</p>
        ${sharedKeys.map(k => {
          const v = shared.get(k);
          return `<div class="field">
            <div class="title" style="margin-bottom:8px;">${esc(v.question)}</div>
            <input type="text" class="iv-a" data-shared="${esc(k)}"
              value="${esc(sharedPrior(k))}" placeholder="one line — every question needs an answer">
            ${v.suggested.length ? `<div class="chips" style="margin-top:8px;">${
              v.suggested.map(s => `<button class="suggest" data-shared="${esc(k)}">${esc(s)}</button>`).join("")}</div>` : ""}
            <div class="sub muted" style="margin-top:6px;">Applies to ${esc(
              Array.from(new Set(v.owners.map(o => nameOf(o.bid)))).join(", "))}</div>
          </div>`;
        }).join("")}
      </div>
    </div>` : "";

  const blocks = ids.map(bid => {
    const iv = interviews[bid] || { questions: [] };
    const prior = {}; (iv.answers || []).forEach(a => prior[a.q] = a.answer);
    // Asked on the Everyone tab instead — never in both places, or answering one would
    // leave the other looking unanswered and the gate would never clear.
    const qs = (iv.questions || []).filter(q => !isShared(bid, q.id)).map(q => `
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
            placeholder="Type it like you'd explain to a new hire — exceptions, preferences, things to avoid">${esc(extra)}</textarea>
        </div>
      </div></div>`;
  }).join("");
  // Frame 09's progress pills: one per agent, "2 of 4", so you can see the whole
  // interview rather than just the page you're on. Display-only on purpose — the frame
  // implies you can jump between agents, but Continue is gated per page, so a clickable
  // pill would let you skip past questions that gate is there to collect.
  //
  // The "Everyone" tab is not built: interviews are strictly per-bundle and there is no
  // shared-question concept to populate it with.
  const answeredCount = (bid) => {
    const iv = interviews[bid] || {};
    const total = (iv.questions || []).length;
    const live = root.querySelectorAll(`.iv-a[data-bid="${bid}"]`);
    if (!live.length && !sharedKeys.length) {
      return { done: total - ((iv.unresolved || []).length), total };
    }
    let done = Array.from(live).filter(i => i.value.trim()).length;
    // A shared answer counts for every agent it covers, or an agent whose questions were
    // all shared would sit at 0 of 3 while fully answered.
    for (const k of sharedKeys) {
      if (!shared.get(k).owners.some(o => o.bid === bid)) continue;
      const f = root.querySelector(`.iv-a[data-shared="${k}"]`);
      if (f && f.value.trim()) done += 1;
    }
    return { done: Math.min(done, total), total };
  };
  const pills = () => {
    // Frame 09 shows Everyone as a pill beside the agents, with "✓ done" of its own.
    const ev = sharedKeys.length ? (() => {
      const done = sharedKeys.filter(k => {
        const f = root.querySelector(`.iv-a[data-shared="${k}"]`);
        return f && f.value.trim();
      }).length;
      const on = pageIds[page] === "__everyone";
      const state = done >= sharedKeys.length ? "done" : (on ? "on" : "");
      return `<span class="iv-pill ${state}">Everyone
        <b>${done >= sharedKeys.length ? "✓" : `${done} of ${sharedKeys.length}`}</b></span>`;
    })() : "";
    return ev + ids.map((bid) => {
      const { done, total } = answeredCount(bid);
      const state = done >= total ? "done" : (pageIds[page] === bid ? "on" : "");
      return `<span class="iv-pill ${state}">${esc(nameOf(bid))}
        <b>${done >= total ? "✓" : `${done} of ${total}`}</b></span>`;
    }).join("");
  };

  const qTotal = ids.reduce((n, bid) => n + ((interviews[bid] || {}).questions || []).length, 0);

  $("#ivBody").innerHTML = `
    ${ids.length ? `<div class="iv-head">
      <div class="iv-count">${qTotal} question${qTotal === 1 ? "" : "s"} for
        ${ids.length} agent${ids.length === 1 ? "" : "s"}</div>
      <div class="iv-pills" id="ivPills">${pills()}</div>
    </div>` : ""}
    ${(everyoneBlock + blocks) || `<p class="muted">No questions needed — you're good to go.</p>`}
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
    const last = page >= pageIds.length - 1;
    const prog = $("#ivProg");
    if (prog) {
      const cur = pageIds[page];
      prog.textContent = pageIds.length > 1
        ? `${page + 1} of ${pageIds.length} — ${cur === "__everyone" ? "Everyone" : nameOf(cur)}`
        : "";
    }
    const back = $("#ivBack");
    if (back) back.style.display = (page > 0) ? "" : "none";
    const go = $("#ivGo");
    if (go) go.textContent = last ? "Save & continue"
      : (pageIds[page] === "__everyone" ? "Next" : "Next workflow");
    updateGo();
    if (typeof window.scrollTo === "function") window.scrollTo(0, 0);
  };
  const updateGo = () => {
    $("#ivGo").disabled = pageInputs().some(inp => !inp.value.trim());
    const pl = $("#ivPills");
    if (pl) pl.innerHTML = pills();          // counts follow what you've typed, not the save
  };
  root.querySelectorAll(".iv-a").forEach(inp => inp.oninput = updateGo);
  root.querySelectorAll(".suggest").forEach(btn => btn.onclick = () => {
    const inp = btn.dataset.shared
      ? root.querySelector(`.iv-a[data-shared="${btn.dataset.shared}"]`)
      : root.querySelector(`.iv-a[data-bid="${btn.dataset.bid}"][data-q="${btn.dataset.q}"]`);
    if (inp) { inp.value = btn.textContent; updateGo(); }
  });
  const back = $("#ivBack");
  if (back) back.onclick = () => { if (page > 0) { page -= 1; showPage(); } };
  showPage();
  const saveAll = async () => {
    const byB = {};
    root.querySelectorAll(".iv-a").forEach(inp => {
      const v = inp.value.trim(); if (!v) return;
      if (inp.dataset.shared) {
        // One field, every agent that asked it — written to each owner's own question id,
        // so the answer lands in the existing per-bundle slot and nothing new is invented
        // to store it.
        for (const o of (shared.get(inp.dataset.shared) || { owners: [] }).owners) {
          (byB[o.bid] = byB[o.bid] || {})[o.qid] = v;
        }
        return;
      }
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
    return byB;                 // the safety net re-posts these alongside its rules
  };
  $("#ivGo").onclick = async () => {
    if (page < pageIds.length - 1) { page += 1; showPage(); return; }   // next page
    $("#ivGo").disabled = true; $("#ivGo").textContent = "Saving…";
    try {
      const saved = await saveAll();          // every page's answers, in one pass
      await refreshSession();                 // pick up interview_complete_at
      // Step 4½ (10) sits between the questions and the build.
      renderSafetyNet(sid, allIds, saved, priorRules);
    } catch (e) {
      const m = $("#ivMsg"); m.className = "msg err"; m.textContent = friendly(e.message);
      $("#ivGo").textContent = "Save & continue"; updateGo();
    }
  };
}

/* ---------------- 04½ SAFETY NET (10) ---------------- */

// The rules that make an agent stop mid-run and come get you. Frame 16 is the other end
// of this screen: when one fires, the run halts and the card names the rule that stopped
// it — "STOPPED — YOUR RULE: NUMBERS THAT LOOK WRONG".
//
// Outbound is first and has no switch on purpose. It is the promise the whole product
// rests on, and a toggle would imply it is negotiable.
// Mirrors SAFETY_RULES_ID in app.py — the reserved answer key the rules ride in on.
const SAFETY_RULES_ID = "safety_rules";

const SAFETY_RULES = [
  { id: "outbound", locked: true, title: "Anything outbound — always",
    sub: "Emails, sheet edits, uploads are staged for your approval. Can't be turned off." },
  { id: "new_party", on: true, title: "Someone new",
    sub: "A contact, portal, or file it hasn't touched before" },
  { id: "odd_numbers", on: true, title: "Numbers that look wrong",
    sub: "Off by more than <b>20%</b> from the usual pattern" },
  { id: "low_confidence", on: true, title: "Low confidence",
    sub: "Not at least 90% sure what you'd do — asks instead of guessing" },
];

// Rules are stored per bundle as one reserved interview answer, so they travel to the
// agent config on the path answers already use. Rendered back as a sentence because that
// is what the config consumes — a list of conditions in the worker's own terms.
function safetyRulesText(on, own) {
  const lines = SAFETY_RULES.filter(r => r.locked || on[r.id])
    .map(r => "- " + r.title.replace(" — always", "") + ": "
              + r.sub.replace(/<[^>]+>/g, "").replace(/\.$/, ""));
  if (own.trim()) lines.push("- " + own.trim());
  return "Stop and ask me when any of these is true:\n" + lines.join("\n");
}

// `saved` is the answers map the interview just posted, keyed by bundle. It has to be
// re-sent with the rules: save_interview REBUILDS a bundle's answer list from whatever it
// is given, so posting the rules alone would drop every question answer and drop the
// bundle back out of "complete", which is the gate the build step stands on.
function renderSafetyNet(sid, ids, saved, prior) {
  clearTimers();
  const on = {};
  SAFETY_RULES.forEach(r => { if (!r.locked) on[r.id] = r.on !== false; });
  let own = "";
  // Coming back to this screen keeps what you set: the stored sentence lists each rule it
  // kept, so a rule missing from it was one you switched off.
  if (prior) {
    SAFETY_RULES.forEach(r => {
      if (!r.locked) on[r.id] = prior.includes(r.title.replace(" — always", ""));
    });
  }

  const row = (r) => r.locked
    ? `<div class="sn-row">
         <div class="sn-mid"><div class="sn-title">${r.title}</div><div class="sn-sub">${r.sub}</div></div>
         <span class="sn-built">BUILT IN</span>
       </div>`
    : `<button class="sn-row sn-toggle" data-rule="${r.id}" aria-pressed="${on[r.id]}">
         <div class="sn-mid"><div class="sn-title">${r.title}</div><div class="sn-sub">${r.sub}</div></div>
         <span class="tgl${on[r.id] ? " on" : ""}"><i></i></span>
       </button>`;

  shell(`
    <div class="sn-stage">
      <div class="eyebrow">Last one — the safety net</div>
      <h1 class="sn-head">When should your agents stop and come get you?</h1>
      <p class="sn-lead">Applies to every agent. Tighten or loosen per agent later, from its profile.</p>
      <div class="sn-list">
        ${SAFETY_RULES.map(row).join("")}
        <button class="sn-add" id="snAdd">+ Add your own — <span>"stop for anything about pricing"</span></button>
        <div class="sn-own" id="snOwnWrap" hidden>
          <textarea id="snOwn" rows="2" placeholder="In your words: stop for anything about pricing"></textarea>
        </div>
      </div>
      <p id="snMsg" class="msg err"></p>
      <div class="sn-acts">
        <button id="snGo" class="btn-primary">Save &amp; build my agents</button>
      </div>
    </div>`);

  root.querySelectorAll(".sn-toggle").forEach(b => b.onclick = () => {
    const id = b.dataset.rule;
    on[id] = !on[id];
    b.setAttribute("aria-pressed", String(on[id]));
    b.querySelector(".tgl").classList.toggle("on", on[id]);
  });
  const wrap = $("#snOwnWrap");
  $("#snAdd").onclick = () => {
    wrap.hidden = false;
    $("#snAdd").hidden = true;
    $("#snOwn").focus();
  };
  $("#snGo").onclick = async () => {
    const btn = $("#snGo");
    btn.disabled = true; btn.textContent = "Saving…";
    own = ($("#snOwn") && $("#snOwn").value) || "";
    try {
      // One save per workflow: the rules are per-agent from here on, so each agent's
      // config carries its own copy and can be loosened later without touching the rest.
      const text = safetyRulesText(on, own);
      for (const bid of ids) {
        const answers = Object.assign({}, (saved || {})[bid], { [SAFETY_RULES_ID]: text });
        const r = await upost(sid, `/interview/${bid}`, { answers });
        if (!r.ok) throw new Error((r.data && r.data.error) || "couldn't save the rules");
      }
      await refreshSession();
      goStep(4);
    } catch (e) {
      $("#snMsg").textContent = friendly(e.message);
      btn.disabled = false; btn.textContent = "Save & build my agents";
    }
  };
}

/* ---------------- 05 BUILD ---------------- */
// Frame 11 ticks off four build phases per agent as they complete. The build job reports
// {state, generated, errors} — a count, with no phase and no current bundle — so those
// ticks would be invented. The phases are shown as what the build is doing, unticked,
// which is the same information without claiming progress we cannot see.
// Frame 11 writes a finished phase in the PAST tense ("Learned the weekly steps") and
// the one in flight in the present. Both forms are kept so a row can change as it
// completes rather than staying hopeful about work already done.
const BUILD_PHASES = [
  { doing: "Learning the steps from your recorded day",
    done: "Learned the steps from your recorded day" },
  { doing: "Writing its judgment rules from your answers",
    done: "Wrote its judgment rules from your answers" },
  { doing: "Testing against your recorded day",
    done: "Tested against your recorded day" },
  { doing: "Safety pass — staging all outbound",
    done: "Safety pass — all outbound staged" },
];

function stepBuild() {
  const sid = S.session.id;
  // Frame 11 centres the whole block vertically (flex:1 + justify-content:center) and
  // keeps the checklist to 360px. At the full content width the rows spread wider than
  // the headline above them and stop reading as centred, and with the block pinned to
  // the top the finished state's button ends up crowding the last line.
  shell(`<div class="rec-stage">
    <div class="bd-mid">
      ${searchMark()}
      <h1 class="rec-head" id="buildHead">Building your agents…</h1>
      <div class="bd-list" id="buildBody"></div>
      <div class="wf-sub">Usually under two minutes.</div>
    </div>
  </div>`);
  runBuild(sid);
}
async function runBuild(sid) {
  const b = $("#buildBody");
  // already built?
  if (S.session.build_at) { b.innerHTML = builtSummary(); wireBuiltContinue(); return; }
  b.innerHTML = `
    <div class="bd-phases">${BUILD_PHASES.map(p =>
      `<div class="bd-phase"><span class="bd-dot"></span>${esc(p.doing)}</div>`).join("")}</div>
    <div id="buildErr" class="msg err"></div>`;
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
  const head = $("#buildHead");
  if (head) head.textContent = "Your agents are built";
  return `
    <div class="bd-phases done">${BUILD_PHASES.map(p =>
      `<div class="bd-phase"><span class="bd-dot on">✓</span>${esc(p.done)}</div>`).join("")}</div>
    <button id="buildGo" class="btn-primary full bd-go">Connect your apps</button>`;
}
function wireBuiltContinue() { $("#buildGo").onclick = () => goStep(5); }

/* ---------------- 06 CONNECT ---------------- */

/* Apps we watched someone use but cannot connect. The backend has always computed
   this list and nothing ever rendered it, so an unsupported app was not merely
   unavailable — it was invisible, and the connect step looked simply empty. Saying
   so is the difference between "palza is broken" and "palza does not support this
   one yet". */
function unmappedNote(unmapped) {
  const list = (unmapped || []).filter(Boolean);
  if (!list.length) return "";
  const names = list.slice(0, 4).map(esc).join(", ")
    + (list.length > 4 ? ` and ${list.length - 4} more` : "");
  return `<p class="notice" style="margin-top:12px;">Palza saw you working in
    <b>${names}</b> but can't connect ${list.length === 1 ? "it" : "them"} yet, so your
    agent won't act there. Everything else above still works.</p>`;
}

async function stepConnect() {
  const sid = S.session.id;
  // Frame 12. The frame captions each app with the agents that need it ("Retail recap ·
  // Inbox triage"); connect.py walks bundles to find apps but collapses the result into a
  // flat by_slug list, dropping which bundle each came from — so that line has no data
  // behind it and is left out rather than guessed.
  shell(`<div class="panel">
    <h1 class="rec-head" style="font-size:26px;">Give your agents their keys</h1>
    <p class="lead">Each agent only gets the apps its workflow needs. Palza never sees your
      password, only uses accounts matching your work email, and you can revoke any of it later.</p>
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
    // Frame 12 captions each app with the workflows that need it, and why it's staged.
    const used = (a.used_by || []).join(" · ");
    return `<div class="card spread"><div><div class="title">${esc(a.label || slug)}</div>
      ${used ? `<div class="sub muted">${esc(used)} — outbound stays staged for you</div>` : ""}
      ${a.flag ? `<div class="sub" style="color:#b06a14;">${esc(a.flag)}</div>` : ""}</div>
      <div class="row">${status}${action}</div></div>`;
  };
  $("#connBody").innerHTML = `
    <div class="field"><span class="label">Your work email</span>
      <div class="row"><input id="connEmail" type="email" value="${esc(email)}" placeholder="the account your agents act as">
        <button id="connEmailGo" class="btn-secondary btn-sm">Confirm</button></div></div>
    <p id="connMsg" class="msg"></p>
    <div class="stack" style="margin-top:8px;">${slugs.map(appCard).join("") || `<p class="muted">No apps to connect for these routines.</p>`}</div>
    ${unmappedNote(rec.unmapped)}
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
    `<button id="connVerify" class="btn-primary full" style="margin-top:10px;">I've approved — verify</button>`);
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
  // Frame 13. The staging controls and #liveGo / #liveMsg are untouched — only the frame
  // around them changes. "Run a test now" is not built: /run-now exists for DEPLOYED
  // agents only, and the frame offers a test run before you commit, which is a different
  // thing than re-running something already live.
  shell(`<div class="panel">
    <h1 class="rec-head" style="font-size:26px;">${S.session.deploy_at
      ? "Your agents are working" : "Meet your agents"}</h1>
    <p class="lead">They'll handle this work on their own from here, starting on the next
      matching task — and they'll show you everything before anything is sent.</p>
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
    // Frame 13 introduces the agent BY NAME — "Meet Retail recap", its avatar in its
    // colour, its schedule, and what happens on the first run. We showed a generic
    // "Ready to go live" tile with no agent on it, even though /deploy-bundles returns
    // the names right here.
    let toDeploy = [];
    try { toDeploy = ((await uget(sid, "/deploy-bundles")).data || {}).bundles || []; }
    catch (e) { /* the generic tile below still stands in */ }
    const startLine = (b) => {
      const v = (b.start || {}).value || "";
      const sched = v ? `Runs ${esc(v)}` : "Runs when you say so";
      return staging.length ? `${sched} · outbound staged for you` : sched;
    };
    const meetCard = (b, i) => `<div class="card" style="text-align:left;display:flex;gap:14px;align-items:center;">
        ${avatar(b.name || b.id, 44, { agent_id: b.id, color: pickShade(
          toDeploy.slice(0, i).map(x => agentColor({ agent_id: x.id }))) })}
        <div style="flex:1;min-width:0;">
          <div class="title" style="font-size:15px;">${esc(b.name || b.id)}</div>
          <div class="sub muted">${startLine(b)}</div>
        </div>
      </div>`;
    const meet = toDeploy.length ? `
      <h1 class="rec-head" style="font-size:22px;margin-bottom:10px;">${toDeploy.length === 1
        ? `Meet ${esc(toDeploy[0].name || toDeploy[0].id)}`
        : `Meet your ${toDeploy.length} agents`}</h1>
      <div class="stack">${toDeploy.map(meetCard).join("")}</div>
      <p class="sub muted" style="margin-top:10px;">${toDeploy.length === 1 ? "It" : "They"}
        start on the next matching task, and will show you everything before anything is
        sent.</p>`
      : `<div class="card center" style="padding:22px;"><div class="big-check">↑</div>
          <div class="title" style="font-size:17px;">Ready to go live</div>
          <div class="notice">They'll start on the next matching task.</div></div>`;
    $("#liveBody").innerHTML = `
      ${meet}
      ${stagingHtml}
      <p id="liveMsg" class="msg err" style="margin-top:12px;"></p>
      <button id="liveGo" class="btn-primary full" style="margin-top:6px;">Go live</button>`;
    $("#liveGo").onclick = async () => {
      $("#liveGo").disabled = true; $("#liveGo").textContent = "Deploying…";
      // Which agents existed BEFORE this deploy, so the ones that appear after it are
      // exactly the ones that need a shade picked (29).
      const before = new Set((((await loadHome()) || {}).agents || []).map(a => a.agent_id));
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
      // Deploy is already done and recorded by here — this only paints the new agents.
      const after = await loadHome(true);
      await assignShadesTo(((after || {}).agents || [])
        .map(a => a.agent_id).filter(id => !before.has(id)));
      await refreshSession(); render();
    };
    return;
  }
  const { data } = await uget(sid, "/deploy-bundles");
  const bundles = data.bundles || [];
  const held = (b) => (b.held_actions || []).map(a => {
    if (a.status !== "held") return `<div class="sub" style="margin-top:6px;">${esc(a.summary)} — ${esc(a.status)}</div>`;
    return `<div class="row" style="margin-top:8px;gap:8px;"><span class="title" style="font-size:13.5px;flex:1;">${esc(a.summary)}</span>
      <button class="btn-primary btn-sm dep-ok" data-b="${esc(b.id)}" data-a="${esc(a.action_id)}">Approve</button>
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
