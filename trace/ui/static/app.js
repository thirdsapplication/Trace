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
const LOCAL_TABS = { dryrun: 1, discdryrun: 1, specdryrun: 1 };  // client-side, no GET

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
  if (tab === "specdryrun") {
    setView(cache[selectedSid].specdryrun || "Click “Spec dry run” to render what the Artifact Engine would be sent.");
    return;
  }
  if (cache[selectedSid][tab] !== undefined) { setView(cache[selectedSid][tab]); return; }

  if (tab === "specs") {       // markdown artifacts, not JSON
    const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/specs`);
    let text;
    if (!ok || !data.specs.length) {
      text = "No specs yet — review bundles, then Generate specs.";
    } else {
      text = data.specs.map(s => `===== ${s.filename} =====\n\n${s.markdown}`).join("\n\n\n");
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
      enableEnvButton(!!art.reviewed);
      reviewData = null; envState = null;
      $("reviewContainer").innerHTML = "";
      $("envContainer").innerHTML = "";
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
let specTimer = null;
const ESC = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function enableReviewButtons(canLoad, loaded) {
  $("loadReviewBtn").disabled = !canLoad;
  $("saveReviewBtn").disabled = !loaded;
  $("genSpecBtn").disabled = !loaded;
  $("specDryRun").disabled = !loaded;
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
    enableEnvButton(true);   // reviewed bundles exist -> can run environment capture
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

$("specDryRun").onclick = async () => {
  $("reviewMsg").textContent = "saving + rendering…";
  await saveReview();
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/specs-dry-run`);
  if (!ok) { $("reviewMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`; return; }
  cache[selectedSid] = cache[selectedSid] || {};
  cache[selectedSid].specdryrun =
    `// SPEC DRY RUN — ${data.approved_count} approved bundle(s), one model call each. No cost.\n\n` +
    `===== SYSTEM PROMPT =====\n${data.system_prompt}\n\n` +
    data.bundles.map(b => `===== BUNDLE ${b.id} =====\n${b.material}`).join("\n\n");
  $("reviewMsg").textContent = "";
  switchTab("specdryrun");
};

$("genSpecBtn").onclick = async () => {
  $("reviewMsg").textContent = "saving + generating…";
  await saveReview();
  const { ok, status, data } = await api("POST", `/api/sessions/${selectedSid}/generate-specs`);
  if (status === 400 && data && data.error === "no_api_key") {
    $("reviewMsg").innerHTML = `<span class="err">no API key</span> — add one in Settings (only the build path needs it).`; return;
  }
  if (!ok && status !== 202) {
    $("reviewMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>`; return;
  }
  setPill("reconstructing");
  pollSpecs();
};

function pollSpecs() {
  if (specTimer) clearInterval(specTimer);
  specTimer = setInterval(async () => {
    const { data } = await api("GET", `/api/sessions/${selectedSid}/generate-specs/status`);
    if (!data) return;
    if (data.state === "done") {
      clearInterval(specTimer); specTimer = null;
      $("reviewMsg").innerHTML = `<span class="ok">done</span> — ${data.built} build spec(s) generated.`;
      setPill("results");
      delete cache[selectedSid];
      await loadAndShow("specs");
      await refreshSessions();
    } else if (data.state === "error") {
      clearInterval(specTimer); specTimer = null;
      const e = data.error === "no_api_key" ? "no API key (add one in Settings)" : data.error;
      $("reviewMsg").innerHTML = `<span class="err">spec generation failed</span> — ${e}`;
      setPill("idle");
    }
  }, 800);
}

// ===========================================================================
// Review 2 — environment capture
// ===========================================================================
let envState = null;   // {approved_ids, environments}

function enableEnvButton(on) { $("loadEnvBtn").disabled = !on; }

$("loadEnvBtn").onclick = async () => {
  if (!selectedSid) return;
  $("envMsg").textContent = "loading…";
  const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/environment-data`);
  if (!ok) {
    $("envMsg").innerHTML = `<span class="err">${(data && data.error) || "failed"}</span>` +
      (data && data.error === "not_reviewed" ? " — save reviewed bundles first." : "");
    return;
  }
  envState = data;
  renderEnv();
  $("envMsg").textContent = "upload the real files, answer the questions, then Save.";
};

function envArtifactRow(bid, idx, a) {
  const ex = a.extracted || {};
  let preview = "";
  if (a.uploaded) {
    const bits = [`format ${ex.format || "?"}`];
    if (ex.headers && ex.headers.length) bits.push(`columns: ${ex.headers.join(", ")}`);
    if (ex.tab_names && ex.tab_names.length) bits.push(`sheets: ${ex.tab_names.join(", ")}`);
    if (ex.field_layout && ex.field_layout.length) bits.push(`fields: ${ex.field_layout.map(f => f.label).join(", ")}`);
    if (ex.note) bits.push(`note: ${ex.note}`);
    preview = `<div class="ex-text">✓ ${ESC(bits.join(" · "))}</div>`;
  }
  return `
  <div class="field" style="flex-direction:column">
    <div><b>${ESC(a.role)}</b> · ${ESC(a.kind)} <span class="muted">— ${ESC(a.label_shown_to_user)}</span>
      <span class="muted">(from ${ESC((a.derived_from_event_ids || []).join(", "))})</span></div>
    <div class="step-row">
      <input type="file" data-bid="${ESC(bid)}" data-idx="${idx}" class="env-file"/>
      ${a.uploaded ? '<span class="ok">uploaded</span>' : '<span class="warn">needed</span>'}
    </div>
    ${preview}
  </div>`;
}

function renderEnv() {
  const c = $("envContainer");
  c.innerHTML = (envState.approved_ids || []).map(bid => {
    const rec = envState.environments[bid] || { artifacts: [], answers: [] };
    const arts = (rec.artifacts || []).map((a, i) => envArtifactRow(bid, i, a)).join("");
    const qs = (rec.answers || []).map((q, i) => `
      <div class="field"><label>${ESC(q.question)}</label>
        <input type="text" class="env-answer" data-bid="${ESC(bid)}" data-qi="${i}" value="${ESC(q.answer)}"/></div>`).join("")
      || `<div class="muted">no questions yet — upload files to derive them</div>`;
    const cls = rec.status === "complete" ? "ok" : "warn";
    return `<div class="review-item bundle" data-bid="${ESC(bid)}">
      <h4>${ESC(bid)} — environment <span class="${cls}">${ESC(rec.status || "incomplete")}</span></h4>
      <div class="review-sub">required artifacts (upload one real example each)</div>
      ${arts || '<div class="muted">no artifacts required</div>'}
      <div class="review-sub">questions</div>
      ${qs}
      <button class="link env-save" data-bid="${ESC(bid)}">save answers</button>
    </div>`;
  }).join("");

  c.querySelectorAll(".env-file").forEach(inp => {
    inp.onchange = () => uploadEnvFile(inp.dataset.bid, parseInt(inp.dataset.idx, 10), inp.files[0]);
  });
  c.querySelectorAll(".env-save").forEach(btn => {
    btn.onclick = () => saveEnvAnswers(btn.dataset.bid);
  });
}

async function uploadEnvFile(bid, idx, file) {
  if (!file) return;
  $("envMsg").textContent = `uploading ${file.name}…`;
  const buf = await file.arrayBuffer();
  const url = `/api/sessions/${selectedSid}/environment/${bid}/upload`
    + `?artifact=${idx}&filename=${encodeURIComponent(file.name)}`;
  const res = await fetch(url, { method: "POST", body: buf });
  const rec = await res.json();
  envState.environments[bid] = rec;
  renderEnv();
  $("envMsg").innerHTML = res.ok ? `<span class="ok">extracted</span> ${ESC(file.name)}`
    : `<span class="err">upload failed</span>`;
  await refreshSessions();
}

async function saveEnvAnswers(bid) {
  const rec = envState.environments[bid];
  $("envContainer").querySelectorAll(`.env-answer[data-bid="${bid}"]`).forEach(inp => {
    const qi = parseInt(inp.dataset.qi, 10);
    if (rec.answers[qi]) rec.answers[qi].answer = inp.value;
  });
  const { ok, data } = await api("POST", `/api/sessions/${selectedSid}/environment/${bid}`, rec);
  if (ok) { envState.environments[bid] = data; renderEnv(); }
  $("envMsg").innerHTML = ok ? `<span class="ok">saved</span> — status ${data.status}`
    : `<span class="err">save failed</span>`;
}

$("refreshPerms").onclick = refreshPermissions;
$("refreshSessions").onclick = refreshSessions;

// ===========================================================================
// Boot
// ===========================================================================
(async function init() {
  await Promise.all([refreshSettings(), refreshPermissions(), refreshSessions(),
                     loadFixtures(), pollStatus()]);
  // If a capture is already running (e.g. page reloaded), resume polling.
  if (capturing) startPolling();
})();
