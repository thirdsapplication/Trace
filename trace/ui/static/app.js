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
const ENDPOINTS = { event_log: "event_log", validation: "validation", raw: "raw" };

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
  if (cache[selectedSid][tab] !== undefined) { setView(cache[selectedSid][tab]); return; }

  const { ok, data } = await api("GET", `/api/sessions/${selectedSid}/${ENDPOINTS[tab]}`);
  let text;
  if (!ok) {
    text = tab === "event_log" || tab === "validation"
      ? "Not reconstructed yet — run Reconstruct."
      : "No raw stream for this session.";
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
  const isJson = activeTab !== "dryrun";
  const blob = new Blob([lastViewText], { type: isJson ? "application/json" : "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${selectedSid || "session"}-${activeTab}.${isJson ? "json" : "txt"}`;
  a.click();
  URL.revokeObjectURL(a.href);
};

// ===========================================================================
// Session history
// ===========================================================================
async function refreshSessions() {
  const { data } = await api("GET", "/api/sessions");
  const tbody = $("sessionsTable").querySelector("tbody");
  tbody.innerHTML = "";
  if (!data || !data.sessions.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">No sessions yet.</td></tr>`;
    return;
  }
  for (const s of data.sessions) {
    const a = s.artifacts || {};
    const tr = document.createElement("tr");
    if (s.id === selectedSid) tr.className = "selected";
    tr.innerHTML = `
      <td><code>${s.id}</code></td>
      <td class="muted">${s.created_at || ""}</td>
      <td class="${a.raw ? "dot-yes" : "dot-no"}">${a.raw ? a.raw_event_count : "—"}</td>
      <td>${a.event_count != null ? a.event_count : "—"}</td>
      <td class="${a.event_log ? "dot-yes" : "dot-no"}">${a.event_log ? "✓" : "—"}</td>
      <td><button class="link" data-open="${s.id}">open</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("[data-open]").forEach(b => {
    b.onclick = async () => {
      selectedSid = b.dataset.open;
      delete cache[selectedSid];
      $("reconTarget").textContent = `target: ${selectedSid}`;
      enableReconButtons(true);
      await loadAndShow("event_log");
      await refreshSessions();
    };
  });
}

$("refreshPerms").onclick = refreshPermissions;
$("refreshSessions").onclick = refreshSessions;

// ===========================================================================
// Boot
// ===========================================================================
(async function init() {
  await Promise.all([refreshSettings(), refreshPermissions(), refreshSessions(), pollStatus()]);
  // If a capture is already running (e.g. page reloaded), resume polling.
  if (capturing) startPolling();
})();
