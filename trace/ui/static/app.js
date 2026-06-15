// Trace Engine 1 test harness — front end (vanilla JS, local only).
"use strict";

const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  return { ok: res.ok, status: res.status, data };
}

// ---- app state -----------------------------------------------------------
let selectedSid = null;       // session currently shown in Results
let activeTab = "event_log";
let capturing = false;
let pollTimer = null;
let reconTimer = null;
let lastViewText = "";        // for copy/export
const cache = {};             // selectedSid -> {event_log, validation, raw, dryrun}

// ---- status pill ---------------------------------------------------------
function setPill(state) {
  const pill = $("statePill");
  pill.className = "pill " + state;
  pill.textContent = state;
}

// ===========================================================================
// Settings
// ===========================================================================
async function refreshSettings() {
  const { data } = await api("GET", "/api/settings");
  if (!data) return;
  const el = $("keyStatus");
  if (data.saved) {
    el.innerHTML = `<span class="ok">✓ key saved</span> (${data.backend}` +
      (data.masked_tail ? `, ${data.masked_tail}` : "") + `)`;
  } else {
    el.innerHTML = `<span class="warn">no key saved</span> — reconstruction needs one`;
  }
}

async function refreshPipedream() {
  const { data } = await api("GET", "/api/settings/pipedream");
  const el = $("pdStatus");
  if (!el) return;
  if (!data) { el.textContent = ""; return; }
  if (data.project_id) $("pdProject").placeholder = data.project_id;
  if (data.client_id_tail) $("pdClientId").placeholder = data.client_id_tail;
  if (data.environment) $("pdEnv").value = data.environment;
  if (data.configured) {
    el.innerHTML = `<span class="ok">✓ Pipedream saved</span> (${data.environment}, ${data.backend})`
      + ` — <span class="muted">run Preflight to confirm it's reachable</span>`;
  } else {
    const have = [];
    if (data.project_id) have.push("project");
    if (data.client_id_tail) have.push("client id");
    if (data.has_secret) have.push("secret");
    el.innerHTML = `<span class="warn">Pipedream not configured</span>` +
      (have.length ? ` — have ${have.join(", ")}; still need the rest` : ` — needed for the live path`);
  }
}

$("savePd").onclick = async () => {
  const body = {
    project_id: $("pdProject").value.trim(),
    client_id: $("pdClientId").value.trim(),
    client_secret: $("pdClientSecret").value,        // not trimmed: secrets can have edges
    environment: $("pdEnv").value,
  };
  $("pdStatus").textContent = "saving…";
  const { ok, data } = await api("POST", "/api/settings/pipedream", body);
  $("pdClientSecret").value = "";                    // never keep the secret in the DOM
  $("pdProject").value = ""; $("pdClientId").value = "";
  if (ok) await refreshPipedream();
  else $("pdStatus").innerHTML = `<span class="err">${(data && data.error) || "save failed"}</span>`;
};

$("saveKey").onclick = async () => {
  const key = $("apiKey").value.trim();
  if (!key) { $("keyStatus").innerHTML = `<span class="err">enter a key first</span>`; return; }
  const { ok, data } = await api("POST", "/api/settings/key", { key });
  $("apiKey").value = "";
  if (ok) { await refreshSettings(); $("testKeyResult").textContent = ""; }
  else $("keyStatus").innerHTML = `<span class="err">${(data && data.error) || "save failed"}</span>`;
};

$("testKey").onclick = async () => {
  $("testKeyResult").textContent = "testing…";
  const { data } = await api("POST", "/api/settings/test-key");
  if (data && data.valid) $("testKeyResult").innerHTML = `<span class="ok">valid ✓</span> ${data.detail}`;
  else $("testKeyResult").innerHTML = `<span class="err">not accepted</span> — ${data ? data.detail : "error"}`;
};

$("preflightBtn").onclick = async () => {
  $("preflightMsg").textContent = "checking…";
  $("preflightChecks").innerHTML = "";
  const { data } = await api("GET", "/api/preflight");
  if (!data) { $("preflightMsg").innerHTML = `<span class="err">preflight failed</span>`; return; }
  $("preflightMsg").innerHTML = data.ok
    ? `<span class="ok">ready for a live run ✓</span>`
    : `<span class="warn">some checks need attention</span>`;
  $("preflightChecks").innerHTML = (data.checks || []).map(c => {
    let mark, cls;
    if (c.ok === true) { mark = "✓"; cls = "ok"; }
    else if (c.ok === false) { mark = "✕"; cls = "err"; }
    else { mark = "–"; cls = "warn"; }       // null -> not configured
    return `<li><span class="mark ${cls}">${mark}</span><b>${ESC(c.name)}</b>
            <span class="muted">${ESC(c.detail || "")}</span></li>`;
  }).join("");
};

async function refreshPermissions() {
  const { data } = await api("GET", "/api/permissions");
  const ul = $("perms");
  if (!data) { ul.innerHTML = "<li>unavailable</li>"; return; }
  const render = (p) => {
    let mark, cls;
    if (p.granted === true) { mark = "✓"; cls = "ok"; }
    else if (p.granted === false) { mark = "✕"; cls = "err"; }
    else { mark = "?"; cls = "warn"; }
    const fix = p.granted === true ? "" : `<div class="help">${p.fix || ""}</div>`;
    return `<li><span class="mark ${cls}">${mark}</span><b>${p.name}</b>
            <span class="muted">${p.detail || ""}</span>${fix}</li>`;
  };
  ul.innerHTML = render(data.accessibility) + render(data.input_monitoring);
}

function renderBrowserStatus(browser) {
  const el = $("browserStatus");
  if (!browser) { el.textContent = "Only reported while capturing."; return; }
  if (browser.detected) {
    el.innerHTML = `<span class="ok">✓ extension sending</span> — ${browser.browser_event_count} events`
      + (browser.last_event_ts ? `, last ${browser.last_event_ts.split("T")[1] || ""}` : "");
  } else {
    el.innerHTML = `<span class="warn">not detected</span> — no browser events yet (local capture still works)`;
  }
}

// ===========================================================================
// Capture
// ===========================================================================
function fmtElapsed(s) {
  if (s == null) return "00:00";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
}

function applyStatus(s) {
  capturing = !!s.running;
  setPill(s.state || (capturing ? "capturing" : "idle"));
  $("elapsed").textContent = fmtElapsed(s.elapsed_seconds);
  $("eventCount").textContent = s.event_count != null ? s.event_count : 0;
  $("captureSid").textContent = s.session_id ? `session ${s.session_id}` : "";
  renderBrowserStatus(s.browser);

  const startStop = $("startStop"), pauseResume = $("pauseResume");
  if (capturing) {
    startStop.textContent = "Stop capture";
    startStop.classList.add("stop");
    pauseResume.disabled = false;
    pauseResume.textContent = s.paused ? "Resume" : "Pause";
  } else {
    startStop.textContent = "Start capture";
    startStop.classList.remove("stop");
    pauseResume.disabled = true;
    pauseResume.textContent = "Pause";
  }
}

async function pollStatus() {
  const { data } = await api("GET", "/api/status");
  if (data) applyStatus(data);
}

$("startStop").onclick = async () => {
  if (!capturing) {
    const { ok, data } = await api("POST", "/api/capture/start");
    if (!ok) {
      const detail = data && (data.detail || data.error) || "could not start";
      alert("Start failed: " + detail +
        (String(detail).includes("pynput") ? "\n\nCapture is macOS-only and needs `pip install pynput`." : ""));
      return;
    }
    applyStatus(data);
    startPolling();
  } else {
    const { ok, data } = await api("POST", "/api/capture/stop");
    stopPolling();
    await pollStatus();
    if (ok && data.session_id) {
      selectedSid = data.session_id;
      $("reconTarget").textContent = `target: ${selectedSid} (${data.raw_event_count} events)`;
      enableReconButtons(true);
      renderCaptureSummary(data.capture_health);
      await loadAndShow("raw");
      await refreshSessions();
      setPill("results");
    }
  }
};

$("pauseResume").onclick = async () => {
  const action = $("pauseResume").textContent === "Pause" ? "pause" : "resume";
  const { ok, data } = await api("POST", "/api/capture/" + action);
  if (ok) applyStatus(data);
};

function startPolling() { stopPolling(); pollTimer = setInterval(pollStatus, 1000); }
function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

function renderCaptureSummary(health) {
  const el = $("captureSummary");
  if (!health) { el.textContent = ""; return; }
  const by = health.by_source || {};
  const parts = Object.keys(by).sort().map(k => `${k}: ${by[k]}`).join(" · ");
  const counts = `<span class="muted">captured — ${parts || "no events"}</span>`;
  if (health.warning) {
    el.innerHTML = `<span class="warn">⚠ ${health.message}</span><br>${counts}`;
  } else {
    el.innerHTML = counts;
  }
}

// ===========================================================================
// Reconstruction
// ===========================================================================
function enableReconButtons(on) {
  $("reconstruct").disabled = !on;
  $("dryRun").disabled = !on;
}

$("dryRun").onclick = async () => {
  if (!selectedSid) return;
  $("reconMsg").textContent = "rendering preview…";
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/dry-run`, {});
  if (!ok) { $("reconMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`; return; }
  cache[selectedSid] = cache[selectedSid] || {};
  const text = `// DRY RUN — ${data.event_count} raw events, ${data.chunk_count} chunk(s)\n` +
    `// This is exactly what Haiku would be sent. No model call, no cost.\n\n` +
    `===== SYSTEM PROMPT =====\n${data.system_prompt}\n\n` +
    data.chunks.map(c => `===== CHUNK ${c.index}/${c.total} =====\n${c.lines.join("\n")}`).join("\n\n");
  cache[selectedSid].dryrun = text;
  $("reconMsg").textContent = "";
  switchTab("dryrun");
};

$("reconstruct").onclick = async () => {
  if (!selectedSid) return;
  await runReconstruct(false);
};

async function runReconstruct(force) {
  $("reconMsg").textContent = "starting…";
  const { ok, status, data } = await api("POST", `/api/sessions/${selectedSid}/reconstruct`, { force });
  if (status === 409 && data && data.warning === "short_recording") {
    if (confirm(`${data.detail}\n\nOnly ${data.event_count} events. Proceed anyway?`)) return runReconstruct(true);
    $("reconMsg").textContent = "cancelled.";
    return;
  }
  if (status === 400 && data && data.error === "no_api_key") {
    $("reconMsg").innerHTML = `<span class="err">no API key</span> — add one in Settings above.`;
    return;
  }
  if (status === 400 && data && data.error === "empty_recording") {
    $("reconMsg").innerHTML = `<span class="err">empty recording</span> — nothing was captured.`;
    return;
  }
  if (!ok && status !== 202) {
    $("reconMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`;
    return;
  }
  setPill("reconstructing");
  showProgress(true);
  pollRecon();
}

function showProgress(on) {
  $("progressWrap").classList.toggle("hidden", !on);
  if (!on) { $("progressBar").style.width = "0"; $("progressText").textContent = ""; }
}

function pollRecon() {
  if (reconTimer) clearInterval(reconTimer);
  reconTimer = setInterval(async () => {
    const { data } = await api("GET", `/api/sessions/${selectedSid}/reconstruct/status`);
    if (!data) return;
    if (data.state === "running") {
      const pct = data.total ? Math.round(100 * data.current / data.total) : 0;
      $("progressBar").style.width = Math.max(4, pct) + "%";
      $("progressText").textContent = data.total ? `part ${data.current} of ${data.total}` : "preparing…";
    } else if (data.state === "done") {
      clearInterval(reconTimer); reconTimer = null;
      showProgress(false);
      $("reconMsg").innerHTML = `<span class="ok">done</span> — ${data.flags} review flag(s), ${data.errors} repair(s).`;
      setPill("results");
      enableDiscoverButtons(true);   // event log now exists -> discovery can run
      $("discoverTarget").textContent = `target: ${selectedSid}`;
      delete cache[selectedSid];
      await loadAndShow("event_log");
      await refreshSessions();
    } else if (data.state === "error") {
      clearInterval(reconTimer); reconTimer = null;
      showProgress(false);
      const e = data.error === "no_api_key" ? "no API key (add one in Settings)" : data.error;
      $("reconMsg").innerHTML = `<span class="err">reconstruction failed</span> — ${e}`;
      setPill("idle");
    }
  }, 700);
}

// ===========================================================================
// Results (JSON views)
// ===========================================================================
const ENDPOINTS = { event_log: "event_log", validation: "validation", raw: "raw",
                    discovery: "discovery", reviewed: "reviewed-bundles" };
const LOCAL_TABS = { dryrun: 1, discdryrun: 1 };  // client-side, no GET

document.querySelectorAll(".tab").forEach(t => {
  t.onclick = () => switchTab(t.dataset.tab);
});

function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  loadAndShow(tab);
}

async function loadAndShow(tab) {
  activeTab = tab;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  $("resultsSid").textContent = selectedSid ? selectedSid : "";
  if (!selectedSid) { setView("Select or create a session."); return; }

  cache[selectedSid] = cache[selectedSid] || {};
  if (tab === "dryrun") {
    setView(cache[selectedSid].dryrun || "Click “Preview (dry run)” to render what Haiku would be sent.");
    return;
  }
  if (tab === "discdryrun") {
    setView(cache[selectedSid].discdryrun || "Click discovery “Preview (dry run)” to render what Sonnet would be sent.");
    return;
  }
  if (cache[selectedSid][tab] !== undefined) { setView(cache[selectedSid][tab]); return; }

  if (tab === "agents") {      // config envelopes + grounding
    const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/agents`);
    let text;
    if (!ok || !data.agents.length) {
      text = "No agents yet — run the interview, then Build agents.";
    } else {
      text = data.agents.map(a =>
        `===== ${a.bundle_id} — agent (test) =====\n${a.test_yaml || "(none)"}\n\n` +
        `===== ${a.bundle_id} — agent (live, NOT deployed) =====\n${a.live_yaml || "(none)"}\n\n` +
        `===== ${a.bundle_id} — grounding =====\n` +
        (a.grounding ? JSON.stringify(a.grounding, null, 2) : "(none)")
      ).join("\n\n\n");
    }
    cache[selectedSid][tab] = text;
    setView(text);
    return;
  }

  const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/${ENDPOINTS[tab]}`);
  let text;
  if (!ok) {
    if (tab === "discovery") text = "Not discovered yet — run Discovery.";
    else if (tab === "reviewed") text = "Not reviewed yet — Load discovery for review, edit, and Save.";
    else if (tab === "event_log" || tab === "validation") text = "Not reconstructed yet — run Reconstruct.";
    else text = "No raw stream for this session.";
  } else {
    text = JSON.stringify(data, null, 2);
  }
  cache[selectedSid][tab] = text;
  setView(text);
}

function setView(text) { lastViewText = text; $("jsonView").textContent = text; }

$("copyBtn").onclick = async () => {
  try { await navigator.clipboard.writeText(lastViewText); $("copyBtn").textContent = "copied"; }
  catch (e) { $("copyBtn").textContent = "copy failed"; }
  setTimeout(() => $("copyBtn").textContent = "copy", 1200);
};

$("exportBtn").onclick = () => {
  if (!lastViewText) return;
  let ext = "json", type = "application/json";
  if (activeTab === "specs") { ext = "md"; type = "text/markdown"; }
  else if (LOCAL_TABS[activeTab] || activeTab === "raw") { ext = "txt"; type = "text/plain"; }
  const blob = new Blob([lastViewText], { type });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${selectedSid || "session"}-${activeTab}.${ext}`;
  a.click();
  URL.revokeObjectURL(a.href);
};

// ===========================================================================
// Discovery (Stage 2)
// ===========================================================================
let discoverTimer = null;
let scoreAvailable = false;   // selected session has an answer key

function enableDiscoverButtons(on) {
  $("discoverBtn").disabled = !on;
  $("discoverDryRun").disabled = !on;
}

async function loadFixtures() {
  const { data } = await api("GET", "/api/fixtures");
  const sel = $("fixtureSelect");
  sel.innerHTML = "";
  if (!data || !data.fixtures.length) {
    sel.innerHTML = `<option value="">(no fixtures)</option>`;
    return;
  }
  for (const f of data.fixtures) {
    const o = document.createElement("option");
    o.value = f.name;
    o.textContent = f.name + (f.has_answer_key ? " ✓key" : "");
    sel.appendChild(o);
  }
}

$("loadFixtureBtn").onclick = async () => {
  const name = $("fixtureSelect").value;
  if (!name) return;
  $("discoverMsg").textContent = "loading fixture…";
  const { ok, data } = await api("POST", "/api/load-fixture", { name });
  if (!ok) { $("discoverMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`; return; }
  selectedSid = data.session_id;
  scoreAvailable = !!data.has_answer_key;
  $("discoverTarget").textContent = `target: ${selectedSid} (fixture ${name}, ${data.event_count} events)`;
  enableDiscoverButtons(true);
  $("scoreBtn").disabled = !scoreAvailable;
  $("scorecardView").classList.add("hidden");
  $("discoverMsg").innerHTML = `loaded fixture <b>${name}</b>` + (scoreAvailable ? " (answer key available)" : "");
  delete cache[selectedSid];
  await loadAndShow("event_log");
  await refreshSessions();
};

$("discoverDryRun").onclick = async () => {
  if (!selectedSid) return;
  $("discoverMsg").textContent = "rendering discovery preview…";
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/discovery-dry-run`);
  if (!ok) { $("discoverMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`; return; }
  cache[selectedSid] = cache[selectedSid] || {};
  cache[selectedSid].discdryrun =
    `// DISCOVERY DRY RUN — ${data.event_count} events, ONE call (no chunking). No cost.\n\n` +
    `===== SYSTEM PROMPT =====\n${data.system_prompt}\n\n` +
    `===== RENDERED EVENT LOG (model input) =====\n${data.rendered_log}`;
  $("discoverMsg").textContent = "";
  switchTab("discdryrun");
};

$("discoverBtn").onclick = async () => {
  if (!selectedSid) return;
  $("discoverMsg").textContent = "starting discovery…";
  const { ok, status, data } = await api("POST", `/api/sessions/${selectedSid}/discover`);
  if (status === 400 && data && data.error === "no_api_key") {
    $("discoverMsg").innerHTML = `<span class="err">no API key</span> — add one in Settings.`; return;
  }
  if (status === 400 && data && data.error === "no_event_log") {
    $("discoverMsg").innerHTML = `<span class="err">no event log</span> — reconstruct or load a fixture first.`; return;
  }
  if (!ok && status !== 202) {
    $("discoverMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`; return;
  }
  setPill("reconstructing");
  $("discoverMsg").innerHTML = `<span class="muted">discovering workflows over the whole day…</span>`;
  pollDiscover();
};

function pollDiscover() {
  if (discoverTimer) clearInterval(discoverTimer);
  discoverTimer = setInterval(async () => {
    const { data } = await api("GET", `/api/sessions/${selectedSid}/discover/status`);
    if (!data) return;
    if (data.state === "done") {
      clearInterval(discoverTimer); discoverTimer = null;
      $("discoverMsg").innerHTML = `<span class="ok">done</span> — ${data.bundles} bundle(s), ${data.anti_bundles} anti-bundle(s).`;
      setPill("results");
      if (scoreAvailable) $("scoreBtn").disabled = false;
      enableReviewButtons(true, false);   // discovery exists -> can load for review
      delete cache[selectedSid];
      await loadAndShow("discovery");
      await refreshSessions();
    } else if (data.state === "error") {
      clearInterval(discoverTimer); discoverTimer = null;
      const e = data.error === "no_api_key" ? "no API key (add one in Settings)" : data.error;
      $("discoverMsg").innerHTML = `<span class="err">discovery failed</span> — ${e}`;
      setPill("idle");
    }
  }, 800);
}

$("scoreBtn").onclick = async () => {
  if (!selectedSid) return;
  $("discoverMsg").textContent = "running gate scorer…";
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/score`);
  if (!ok) {
    const e = (data && (data.detail || data.error)) || "failed";
    $("discoverMsg").innerHTML = `<span class="err">score failed</span> — ${e}`; return;
  }
  $("discoverMsg").textContent = "";
  renderScorecard(data);
};

function renderScorecard(sc) {
  const v = $("scorecardView");
  v.classList.remove("hidden");
  const verdict = sc.passed === null ? "" : (sc.passed ? "  VERDICT: PASS" : "  VERDICT: FAIL");
  v.textContent =
    `GATE SCORECARD\n` +
    `bundles identified: ${sc.bundles_identified} of ${sc.bundles_total}  (overlap >= ${sc.overlap_threshold})\n` +
    `automation penalties: ${sc.automation_penalties}\n` +
    `sensitivity: ${Object.entries(sc.sensitivity || {}).map(([t, n]) => `${t}:${n}`).join(", ")}\n` +
    verdict + "\n\n" + JSON.stringify(sc, null, 2);
}

// ===========================================================================
// Session history
// ===========================================================================
async function refreshSessions() {
  const { data } = await api("GET", "/api/sessions");
  const tbody = $("sessionsTable").querySelector("tbody");
  tbody.innerHTML = "";
  if (!data || !data.sessions.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">No sessions yet.</td></tr>`;
    return;
  }
  for (const s of data.sessions) {
    const a = s.artifacts || {};
    const tr = document.createElement("tr");
    if (s.id === selectedSid) tr.className = "selected";
    const disc = a.discovery ? `✓ ${a.candidate_count != null ? a.candidate_count : ""}` : "—";
    tr.innerHTML = `
      <td><code>${s.id}</code>${s.fixture ? ` <span class="muted">(${s.fixture})</span>` : ""}</td>
      <td class="muted">${s.created_at || ""}</td>
      <td class="${a.raw ? "dot-yes" : "dot-no"}">${a.raw ? a.raw_event_count : "—"}</td>
      <td>${a.event_count != null ? a.event_count : "—"}</td>
      <td class="${a.event_log ? "dot-yes" : "dot-no"}">${a.event_log ? "✓" : "—"}</td>
      <td class="${a.discovery ? "dot-yes" : "dot-no"}">${disc}</td>
      <td><button class="link" data-open="${s.id}" data-ak="${s.answer_key_path ? 1 : 0}">open</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("[data-open]").forEach(b => {
    b.onclick = async () => {
      selectedSid = b.dataset.open;
      delete cache[selectedSid];
      $("reconTarget").textContent = `target: ${selectedSid}`;
      enableReconButtons(true);
      // a session with an event log can be discovered; with an answer key, scored
      const meta = await api("GET", `/api/sessions/${selectedSid}`);
      const art = (meta.data && meta.data.artifacts) || {};
      enableDiscoverButtons(!!art.event_log);
      scoreAvailable = !!(meta.data && meta.data.answer_key_path);
      $("scoreBtn").disabled = !(scoreAvailable && art.discovery);
      enableReviewButtons(!!art.discovery, false);
      enableInterview(!!art.reviewed);
      enableBuild(!!art.reviewed);
      enableConnect(!!art.reviewed);
      enableDeploy(!!art.reviewed);
      reviewData = null; interviewState = null; $("buildSummary").innerHTML = "";
      $("reviewContainer").innerHTML = "";
      $("interviewContainer").innerHTML = "";
      $("discoverTarget").textContent = art.event_log ? `target: ${selectedSid}` : "";
      $("scorecardView").classList.add("hidden");
      await loadAndShow("event_log");
      await refreshSessions();
    };
  });
}

// ===========================================================================
// Stage 3 — Review / edit + spec generation
// ===========================================================================
let reviewData = null;        // {reviewed, examples_by_id}
const ESC = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function enableReviewButtons(canLoad, loaded) {
  $("loadReviewBtn").disabled = !canLoad;
  $("saveReviewBtn").disabled = !loaded;
}

$("loadReviewBtn").onclick = async () => {
  if (!selectedSid) return;
  $("reviewMsg").textContent = "loading…";
  const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/review-data`);
  if (!ok) {
    $("reviewMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>` +
      (data && data.error === "not_discovered" ? " — run Discovery first." : "");
    return;
  }
  reviewData = data;
  renderReview();
  enableReviewButtons(true, true);
  $("reviewMsg").textContent = "loaded — edit, then Save.";
};

function itemCard(item, isAnti) {
  const ex = reviewData.examples_by_id || {};
  const steps = item.steps || [];
  const cpByStep = {};
  (item.human_checkpoints || []).forEach(c => { cpByStep[c.step_index] = c.note || ""; });
  const included = new Set(item.included_example_event_ids || []);
  const exIds = item.evidence_event_ids || [];

  const stepRows = steps.map((s, i) => `
    <div class="step-row">
      <input type="text" class="step-text" value="${ESC(s)}" />
      <label><input type="checkbox" class="step-cp" ${cpByStep[i] !== undefined ? "checked" : ""}/> checkpoint</label>
      <input type="text" class="cp-note" placeholder="pause-and-ask note" value="${ESC(cpByStep[i] || "")}" />
    </div>`).join("");

  const exRows = exIds.map(id => `
    <div class="ex-row">
      <label><input type="checkbox" class="ex-incl" data-eid="${ESC(id)}" ${included.has(id) ? "checked" : ""}/> ${ESC(id)}</label>
      <span class="ex-text">${ESC(ex[id] ? ex[id].detail : "(example text unavailable)")}</span>
    </div>`).join("") || `<div class="muted">no examples</div>`;

  return `
  <div class="review-item ${isAnti ? "anti" : "bundle"}" data-id="${ESC(item.id)}"
       data-orig-class="${isAnti ? "anti_bundle" : "bundle"}"
       data-orig-why="${ESC(item.why_judgment || "")}">
    <h4>${ESC(item.id)} · <span class="muted">${item.confidence || ""}</span></h4>
    <div class="field"><label>name</label><input type="text" class="f-name" value="${ESC(item.name)}"/></div>
    <div class="field"><label>classification</label>
      <select class="f-class">
        <option value="bundle" ${!isAnti ? "selected" : ""}>bundle (automatable)</option>
        <option value="anti_bundle" ${isAnti ? "selected" : ""}>anti-bundle (judgment)</option>
      </select>
      <label>disposition</label>
      <select class="f-disp">
        <option value="approve_for_build" ${item.disposition !== "reject" ? "selected" : ""}>approve_for_build</option>
        <option value="reject" ${item.disposition === "reject" ? "selected" : ""}>reject</option>
      </select>
    </div>
    <div class="field"><label>description</label><textarea class="f-desc">${ESC(item.description)}</textarea></div>
    <div class="field"><label>trigger</label><input type="text" class="f-trigger" value="${ESC(item.trigger || "")}"/></div>
    <div class="field"><label>why judgment</label><textarea class="f-why" placeholder="(for anti-bundle / reclassification)">${ESC(item.why_judgment || "")}</textarea></div>
    <div class="field"><label>handling</label><input type="text" class="f-handling" value="${ESC(item.recommended_handling || "")}"/></div>
    <div class="review-sub">steps (toggle a checkpoint per step)</div>
    <div class="steps">${stepRows}</div>
    <button class="link add-step">+ add step</button>
    <div class="review-sub">observed examples (include / exclude — verbatim)</div>
    ${exRows}
    <div class="review-sub">added acceptance criteria (one per line)</div>
    <div class="field"><textarea class="f-criteria">${ESC((item.added_acceptance_criteria || []).join("\n"))}</textarea></div>
    <div class="review-sub">preferred output (optional)</div>
    <div class="field"><textarea class="f-prefout">${ESC(item.preferred_output || "")}</textarea></div>
  </div>`;
}

function renderReview() {
  const c = $("reviewContainer");
  const r = reviewData.reviewed;
  c.innerHTML =
    (r.bundles || []).map(b => itemCard(b, false)).join("") +
    (r.anti_bundles || []).map(a => itemCard(a, true)).join("");
  c.querySelectorAll(".add-step").forEach(btn => {
    btn.onclick = () => {
      const steps = btn.previousElementSibling;
      const div = document.createElement("div");
      div.className = "step-row";
      div.innerHTML = `<input type="text" class="step-text" value=""/>
        <label><input type="checkbox" class="step-cp"/> checkpoint</label>
        <input type="text" class="cp-note" placeholder="pause-and-ask note"/>`;
      steps.appendChild(div);
    };
  });
}

function scrapeReview() {
  const bundles = [], anti = [];
  $("reviewContainer").querySelectorAll(".review-item").forEach(el => {
    const cls = el.querySelector(".f-class").value;
    const origClass = el.dataset.origClass;
    const steps = [], checkpoints = [];
    el.querySelectorAll(".step-row").forEach((row, i) => {
      const txt = row.querySelector(".step-text").value.trim();
      if (!txt) return;
      const idx = steps.length;
      steps.push(txt);
      if (row.querySelector(".step-cp").checked)
        checkpoints.push({ step_index: idx, note: row.querySelector(".cp-note").value.trim() });
    });
    const incl = [];
    el.querySelectorAll(".ex-incl").forEach(cb => { if (cb.checked) incl.push(cb.dataset.eid); });
    const crit = el.querySelector(".f-criteria").value.split("\n").map(s => s.trim()).filter(Boolean);
    const item = {
      id: el.dataset.id, classification: cls,
      name: el.querySelector(".f-name").value.trim(),
      description: el.querySelector(".f-desc").value.trim(),
      trigger: el.querySelector(".f-trigger").value.trim(),
      steps,
      disposition: el.querySelector(".f-disp").value,
      human_checkpoints: checkpoints,
      included_example_event_ids: incl,
      added_acceptance_criteria: crit,
      preferred_output: el.querySelector(".f-prefout").value.trim(),
      why_judgment: el.querySelector(".f-why").value.trim(),
      recommended_handling: el.querySelector(".f-handling").value.trim(),
      evidence_event_ids: incl.slice(),  // provenance kept; server filters to known ids
      reclassified_from: cls !== origClass ? origClass : null,
      original_reason_for_judgment: (cls === "bundle" && origClass === "anti_bundle")
        ? el.dataset.origWhy : "",
    };
    // keep full evidence provenance (union of original examples)
    el.querySelectorAll(".ex-incl").forEach(cb => {
      if (!item.evidence_event_ids.includes(cb.dataset.eid)) item.evidence_event_ids.push(cb.dataset.eid);
    });
    (cls === "anti_bundle" ? anti : bundles).push(item);
  });
  return { bundles, anti_bundles: anti, notes: (reviewData.reviewed.notes || "") };
}

async function saveReview() {
  const body = scrapeReview();
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/reviewed-bundles`, body);
  if (ok) {
    reviewData.reviewed = data.reviewed;
    enableInterview(true); enableBuild(true); enableConnect(true); enableDeploy(true);
    delete cache[selectedSid];
    await refreshSessions();
  }
  return { ok, data };
}

$("saveReviewBtn").onclick = async () => {
  $("reviewMsg").textContent = "saving…";
  const { ok, data } = await saveReview();
  $("reviewMsg").innerHTML = ok
    ? `<span class="ok">saved</span> — ${data.errors.length} repair(s), ${data.flags.length} flag(s).`
    : `<span class="err">save failed</span>`;
};

// ===========================================================================
// Phase 2 — Structured interview (gap detector)
// ===========================================================================
let interviewState = null;   // {approved_ids, interviews}
let interviewTimer = null;

function enableInterview(on) {
  $("generateQBtn").disabled = !on;
  $("intDryRun").disabled = !on;
  if (on) loadInterview(); else $("interviewContainer").innerHTML = "";
}

async function loadInterview() {
  if (!selectedSid) return;
  const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/interview-data`);
  if (!ok) return;
  interviewState = data;
  renderInterview();
}

$("generateQBtn").onclick = async () => {
  if (!selectedSid) return;
  $("interviewMsg").textContent = "generating questions…";
  const { ok, status, data } = await api("POST", `/api/sessions/${selectedSid}/interview`, {});
  if (status === 400 && data && data.error === "no_api_key") {
    $("interviewMsg").innerHTML = `<span class="err">no API key</span> — add one in Settings.`; return;
  }
  if (status === 400 && data && data.error === "not_reviewed") {
    $("interviewMsg").innerHTML = `<span class="err">save reviewed bundles first</span>`; return;
  }
  if (!ok && status !== 202) {
    $("interviewMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`; return;
  }
  setPill("reconstructing");
  pollInterview();
};

function pollInterview() {
  if (interviewTimer) clearInterval(interviewTimer);
  interviewTimer = setInterval(async () => {
    const { data } = await api("GET", `/api/sessions/${selectedSid}/interview/status`);
    if (!data) return;
    if (data.state === "done") {
      clearInterval(interviewTimer); interviewTimer = null;
      $("interviewMsg").innerHTML = `<span class="ok">done</span> — questions for ${data.generated} bundle(s).`;
      setPill("results");
      await loadInterview();
      await refreshSessions();
    } else if (data.state === "error") {
      clearInterval(interviewTimer); interviewTimer = null;
      const e = data.error === "no_api_key" ? "no API key (add one in Settings)" : data.error;
      $("interviewMsg").innerHTML = `<span class="err">interview failed</span> — ${e}`;
      setPill("idle");
    }
  }, 800);
}

$("intDryRun").onclick = async () => {
  if (!selectedSid) return;
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/interview-dry-run`, {});
  if (!ok) { $("interviewMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`; return; }
  cache[selectedSid] = cache[selectedSid] || {};
  alert("Gap Detector — system prompt + per-bundle material (no model call):\n\n" +
    data.bundles.map(b => `===== ${b.id} =====\n${b.material}`).join("\n\n").slice(0, 4000));
};

function questionRow(bid, q, answer) {
  const chips = (q.suggested_answers || []).map(s =>
    `<button class="link int-chip" data-bid="${ESC(bid)}" data-q="${ESC(q.id)}">${ESC(s)}</button>`).join(" ");
  return `<div class="field" style="flex-direction:column">
    <div><b>${ESC(q.question)}</b>
      <span class="muted">[${ESC(q.lens)} · ${ESC(q.cost_if_wrong)} cost]</span></div>
    <div class="muted">${ESC(q.why_it_matters)}</div>
    <div class="step-row">
      <input type="text" class="int-answer" data-bid="${ESC(bid)}" data-q="${ESC(q.id)}"
        placeholder="one line — or leave blank to skip" value="${ESC(answer)}"/>
    </div>
    ${chips ? `<div class="muted">suggestions: ${chips}</div>` : ""}
  </div>`;
}

function renderInterview() {
  const c = $("interviewContainer");
  c.innerHTML = (interviewState.approved_ids || []).map(bid => {
    const rec = interviewState.interviews[bid] || { questions: [], answers: [], status: "unstarted" };
    const ansById = {};
    (rec.answers || []).forEach(a => { ansById[a.q] = a.answer; });
    const qs = (rec.questions || []).map(q => questionRow(bid, q, ansById[q.id] || "")).join("")
      || `<div class="muted">no questions — capture already settled this workflow</div>`;
    const cls = rec.status === "complete" ? "ok" : "warn";
    return `<div class="review-item bundle" data-bid="${ESC(bid)}">
      <h4>${ESC(bid)} — interview <span class="${cls}">${ESC(rec.status || "unstarted")}</span></h4>
      ${qs}
      <button class="link int-save" data-bid="${ESC(bid)}">save answers</button>
    </div>`;
  }).join("");

  c.querySelectorAll(".int-chip").forEach(btn => {
    btn.onclick = () => {
      const inp = c.querySelector(`.int-answer[data-bid="${btn.dataset.bid}"][data-q="${btn.dataset.q}"]`);
      if (inp) inp.value = btn.textContent;
    };
  });
  c.querySelectorAll(".int-save").forEach(btn => {
    btn.onclick = () => saveInterview(btn.dataset.bid);
  });
}

async function saveInterview(bid) {
  const answers = {};
  $("interviewContainer").querySelectorAll(`.int-answer[data-bid="${bid}"]`).forEach(inp => {
    if (inp.value.trim()) answers[inp.dataset.q] = inp.value.trim();
  });
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/interview/${bid}`, { answers });
  if (ok) {
    interviewState.interviews[bid] = data;
    renderInterview();
    $("interviewMsg").innerHTML = `<span class="ok">saved ${ESC(bid)}</span> — status ${ESC(data.status)}` +
      (data.unresolved.length ? `, ${data.unresolved.length} skipped` : "");
  } else {
    $("interviewMsg").innerHTML = `<span class="err">save failed</span>`;
  }
  await refreshSessions();
}

// ===========================================================================
// Build agents — config (Phase 1)
// ===========================================================================
let buildTimer = null;

function enableBuild(on) {
  $("buildBtn").disabled = !on;
  if (on) loadBuildBundles(); else $("buildBundles").innerHTML = "";
}

async function loadBuildBundles() {
  if (!selectedSid) return;
  const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/build-bundles`);
  const el = $("buildBundles");
  if (!ok || !data.bundles || !data.bundles.length) { el.innerHTML = ""; return; }
  el.innerHTML = data.bundles.map(b => {
    const chip = b.built ? ` <span class="ok">built</span>` : "";
    return `<label><input type="checkbox" class="bld" value="${ESC(b.id)}" checked>` +
           `<b>${ESC(b.id)}</b> ${ESC(b.name)}${chip}</label>`;
  }).join("");
}

$("buildBtn").onclick = async () => {
  if (!selectedSid) return;
  const ids = Array.from(document.querySelectorAll("#buildBundles .bld:checked")).map(c => c.value);
  if (!ids.length) { $("buildMsg").innerHTML = `<span class="warn">select at least one bundle</span>`; return; }
  $("buildMsg").textContent = "building…";
  const { ok, status, data } = await api("POST", `/api/sessions/${selectedSid}/build`, { bundle_ids: ids });
  if (status === 400 && data && data.error === "no_api_key") {
    $("buildMsg").innerHTML = `<span class="err">no API key</span> — add one in Settings.`; return;
  }
  if (status === 400 && data && data.error === "not_reviewed") {
    $("buildMsg").innerHTML = `<span class="err">save reviewed bundles first</span>`; return;
  }
  if (!ok && status !== 202) {
    $("buildMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`; return;
  }
  setPill("reconstructing");
  pollBuild();
};

function pollBuild() {
  if (buildTimer) clearInterval(buildTimer);
  buildTimer = setInterval(async () => {
    const { data } = await api("GET", `/api/sessions/${selectedSid}/build/status`);
    if (!data) return;
    if (data.state === "done") {
      clearInterval(buildTimer); buildTimer = null;
      const errs = Object.keys(data.errors || {}).length;
      $("buildMsg").innerHTML = `<span class="ok">done</span> — ${data.generated} built` +
        (errs ? ` · <span class="err">${errs} error(s)</span>` : "");
      setPill("results");
      delete cache[selectedSid];
      loadBuildBundles();
      await loadAndShow("agents");
      await refreshSessions();
    } else if (data.state === "error") {
      clearInterval(buildTimer); buildTimer = null;
      $("buildMsg").innerHTML = `<span class="err">build failed</span> — ${data.error}`;
      setPill("idle");
    }
  }, 800);
}

$("cleanupBtn").onclick = async () => {
  if (!selectedSid) return;
  if (!confirm("Delete the sessions, files, and agents Trace created on the platform for this session? This removes them from the Console.")) return;
  $("cleanupMsg").textContent = "cleaning up…";
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/cleanup-platform`);
  if (!ok || (data && data.error)) {
    $("cleanupMsg").innerHTML = `<span class="err">${ESC((data && data.error) || "failed")}</span>`; return;
  }
  const rm = data.removed || { sessions: [], files: [], agents: [] };
  const errs = (data.errors || []).length ? ` · ${data.errors.length} error(s)` : "";
  $("cleanupMsg").innerHTML = `<span class="ok">removed</span> — ` +
    `${rm.sessions.length} session(s), ${rm.files.length} file(s), ${rm.agents.length} agent(s)${errs}`;
};

// ===========================================================================
// Phase 4 — Connect your apps (Pipedream Connect + MA vault)
// ===========================================================================
let connectTimer = null;

function enableConnect(on) {
  $("saveEmailBtn").disabled = !on;
  if (on) loadConnect(); else { $("connectApps").innerHTML = ""; $("connectExpectation").textContent = ""; }
}

async function loadConnect() {
  if (!selectedSid) return;
  const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/connection-data`);
  if (!ok) return;
  $("connectExpectation").textContent = data.expectation || "";
  const rec = data.record || { apps: {}, expected_identity: "" };
  if (!$("workerEmail").value) $("workerEmail").value = rec.expected_identity || "";
  renderConnectApps(rec, data.unmapped || []);
}

function renderConnectApps(rec, unmapped) {
  const el = $("connectApps");
  const slugs = Object.keys(rec.apps || {});
  if (!slugs.length) { el.innerHTML = `<div class="muted">No connectable apps in the approved bundles yet.</div>`; return; }
  el.innerHTML = slugs.map(slug => {
    const a = rec.apps[slug];
    let status;
    if (!a.connected) status = `<span class="muted">not connected</span>`;
    else if (a.verified === "yes") status = `<span class="ok">connected ✓ (${ESC((a.account || {}).identity_email || "")})</span>`;
    else status = `<span class="err">connected — FLAGGED ⚠ not used</span>`;
    const flag = (a.flag && a.verified !== "yes") ? `<div class="ex-text err">${ESC(a.flag)}</div>` : "";
    return `<div class="field" style="flex-direction:column">
      <div><b>${ESC(a.label || slug)}</b> — ${status}</div>
      <div class="step-row">
        <button class="link conn-start" data-slug="${ESC(slug)}">Connect</button>
        <button class="link conn-verify" data-slug="${ESC(slug)}">Verify connection</button>
      </div>${flag}</div>`;
  }).join("") + (unmapped.length
    ? `<div class="muted">Observed apps with no connector (skipped): ${unmapped.map(ESC).join(", ")}</div>` : "");
  el.querySelectorAll(".conn-start").forEach(b => b.onclick = () => connectStart(b.dataset.slug));
  el.querySelectorAll(".conn-verify").forEach(b => b.onclick = () => connectVerify(b.dataset.slug));
}

$("saveEmailBtn").onclick = async () => {
  if (!selectedSid) return;
  const email = $("workerEmail").value.trim();
  if (!email) { $("connectMsg").innerHTML = `<span class="warn">enter your work email</span>`; return; }
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/connection-identity`, { email });
  $("connectMsg").innerHTML = ok ? `<span class="ok">email confirmed</span>`
    : `<span class="err">${ESC((data && data.error) || "failed")}</span>`;
};

async function connectStart(slug) {
  $("connectMsg").textContent = `opening Connect for ${slug}…`;
  const { ok, status, data } = await api("POST", `/api/sessions/${selectedSid}/connect/${slug}`, {});
  if (status === 400 && data && data.error === "no_api_key") {
    $("connectMsg").innerHTML = `<span class="err">no API key</span> — add one in Settings.`; return; }
  if (!ok && status !== 202) { $("connectMsg").innerHTML = `<span class="err">${ESC((data && data.error) || "failed")}</span>`; return; }
  pollConnect(d => {
    if (d.connect_url) {
      window.open(d.connect_url, "_blank", "noopener");
      $("connectMsg").innerHTML = `Authorize <b>${ESC(slug)}</b> in the window that opened — `
        + `then come back and tap <b>Verify connection</b>. `
        + `<span class="muted">(didn't open? <a href="${ESC(d.connect_url)}" target="_blank" rel="noopener">open it here</a>)</span>`;
    } else {
      $("connectMsg").innerHTML = `<span class="warn">no Connect URL returned</span> — check Preflight.`;
    }
  });
}

async function connectVerify(slug) {
  $("connectMsg").textContent = `verifying ${slug}…`;
  const { ok, status, data } = await api("POST", `/api/sessions/${selectedSid}/connect/${slug}/callback`, {});
  if (!ok && status !== 202) { $("connectMsg").innerHTML = `<span class="err">${ESC((data && data.error) || "failed")}</span>`; return; }
  pollConnect(d => {
    if (d.verified === "yes") {
      $("connectMsg").innerHTML = `<span class="ok">${ESC(slug)} connected & verified</span>`;
    } else if (d.connected) {
      $("connectMsg").innerHTML = `<span class="err">${ESC(slug)} FLAGGED</span> — ${ESC(d.flag || "not verified")}`;
    } else {
      $("connectMsg").innerHTML = `<span class="warn">${ESC(slug)} not connected yet</span> — `
        + `tap <b>Connect</b> and finish Google's authorization first, then Verify.`;
    }
    loadConnect();
  });
}

function pollConnect(onDone) {
  if (connectTimer) clearInterval(connectTimer);
  connectTimer = setInterval(async () => {
    const { data } = await api("GET", `/api/sessions/${selectedSid}/connect/status`);
    if (!data) return;
    if (data.state === "done") { clearInterval(connectTimer); connectTimer = null; onDone(data); }
    else if (data.state === "error") {
      clearInterval(connectTimer); connectTimer = null;
      $("connectMsg").innerHTML = `<span class="err">connection failed</span> — ${ESC(data.error)}`;
    }
  }, 600);
}

// ===========================================================================
// Phase 6 — Go live (deploy over the connection; outbound held for approval)
// ===========================================================================
let deployTimer = null;

$("startMode").onchange = () => {
  $("scheduleInput").style.display = $("startMode").value === "schedule" ? "" : "none";
};

function enableDeploy(on) {
  $("deployBtn").disabled = !on;
  if (on) loadDeploy(); else $("deployContainer").innerHTML = "";
}

async function loadDeploy() {
  if (!selectedSid) return;
  const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/deploy-bundles`);
  const el = $("deployContainer");
  if (!ok || !data.bundles || !data.bundles.length) {
    el.innerHTML = `<div class="muted">No bundles ready — needs a built config and a usable connection.</div>`;
    return;
  }
  el.innerHTML = data.bundles.map(b => {
    const status = b.status === "live" ? `<span class="ok">live ✓</span>`
      : b.status ? `<span class="warn">${ESC(b.status)}</span>` : `<span class="muted">not deployed</span>`;
    const start = b.start ? ` <span class="muted">on ${ESC(b.start.mode)}: ${ESC(b.start.value || "")}</span>` : "";
    const held = (b.held_actions || []).map(a => {
      if (a.status !== "held") return `<div class="run-line">&nbsp;&nbsp;${ESC(a.summary)} — <span class="${a.status === "approved" ? "ok" : "muted"}">${ESC(a.status)}</span></div>`;
      return `<div class="run-line">&nbsp;&nbsp;<b>hold:</b> ${ESC(a.summary)} (${ESC(a.kind)})
        <button class="link dep-ok" data-bid="${ESC(b.id)}" data-a="${ESC(a.action_id)}">Approve</button>
        <button class="link dep-no" data-bid="${ESC(b.id)}" data-a="${ESC(a.action_id)}">Reject</button></div>`;
    }).join("") || (b.status ? `<div class="run-line muted">&nbsp;&nbsp;no outbound actions held</div>` : "");
    return `<div class="review-item bundle" data-bid="${ESC(b.id)}">
      <h4>${ESC(b.id)} — ${ESC(b.name)} ${status}${start}</h4>${held}</div>`;
  }).join("");
  el.querySelectorAll(".dep-ok").forEach(btn => btn.onclick = () => deployResolve(btn.dataset.bid, btn.dataset.a, "approve"));
  el.querySelectorAll(".dep-no").forEach(btn => btn.onclick = () => deployResolve(btn.dataset.bid, btn.dataset.a, "reject"));
}

$("deployBtn").onclick = async () => {
  if (!selectedSid) return;
  const mode = $("startMode").value;
  const body = mode === "schedule" ? { schedule: $("scheduleInput").value.trim() } : { trigger: "on a new matching event" };
  $("deployMsg").textContent = "deploying live…";
  const { ok, status, data } = await api("POST", `/api/sessions/${selectedSid}/deploy`, body);
  if (status === 400 && data && data.error === "not_eligible") {
    $("deployMsg").innerHTML = `<span class="warn">not ready</span> — build a config and connect a usable app first.`; return;
  }
  if (status === 400 && data && data.error === "no_api_key") {
    $("deployMsg").innerHTML = `<span class="err">no API key</span> — add one in Settings.`; return;
  }
  if (!ok && status !== 202) { $("deployMsg").innerHTML = `<span class="err">${ESC((data && data.error) || "failed")}</span>`; return; }
  setPill("reconstructing");
  pollDeploy();
};

function pollDeploy() {
  if (deployTimer) clearInterval(deployTimer);
  deployTimer = setInterval(async () => {
    const { data } = await api("GET", `/api/sessions/${selectedSid}/deploy/status`);
    if (!data) return;
    if (data.state === "done") {
      clearInterval(deployTimer); deployTimer = null;
      const errs = Object.keys(data.errors || {}).length;
      $("deployMsg").innerHTML = `<span class="ok">live</span> — ${data.deployed} deployed` +
        (errs ? ` · <span class="err">${errs} error(s)</span>` : "");
      setPill("results");
      loadDeploy();
      delete cache[selectedSid];
    } else if (data.state === "error") {
      clearInterval(deployTimer); deployTimer = null;
      $("deployMsg").innerHTML = `<span class="err">deploy failed</span> — ${ESC(data.error)}`;
      setPill("idle");
    }
  }, 800);
}

async function deployResolve(bid, actionId, decision) {
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/deploy/${bid}/${decision}`,
    { action_id: actionId });
  if (!ok || (data && data.error)) {
    $("deployMsg").innerHTML = `<span class="err">${ESC((data && data.error) || "failed")}</span>`; return;
  }
  $("deployMsg").innerHTML = `<span class="ok">${ESC(data.status)}</span> — ${ESC(actionId)}`;
  loadDeploy();
}

$("refreshPerms").onclick = refreshPermissions;
$("refreshSessions").onclick = refreshSessions;

// ===========================================================================
// Boot
// ===========================================================================
(async function init() {
  await Promise.all([refreshSettings(), refreshPipedream(), refreshPermissions(),
                     refreshSessions(), loadFixtures(), pollStatus()]);
  // If a capture is already running (e.g. page reloaded), resume polling.
  if (capturing) startPolling();
})();
