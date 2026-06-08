// Trace capture extension — background service worker (Manifest V3).
//
// Responsibilities:
//  - relay normalized raw events from content scripts to the local collector
//    (http://127.0.0.1:8137/event), reliably under MV3's ephemeral service worker;
//  - announce presence (a heartbeat) so the collector can report "extension
//    connected" even before the first click;
//  - own the pause/resume toggle (toolbar action) and expose it to content scripts.
//
// MV3 correctness note: a service worker is terminated after ~30s idle. A
// `setInterval` timer neither survives termination nor keeps the worker alive, and
// module-global state is lost on termination — so a "queue + setInterval flush"
// design silently drops sporadic events (clicks, structured reads). Instead we
// POST *while the worker is awake* by handling each message with an async response
// (which keeps the worker alive until the fetch settles), and use `chrome.alarms`
// (which DO survive termination) only to retry events that failed while the
// collector was briefly down.

const COLLECTOR_URL = "http://127.0.0.1:8137/event";
const HELLO_URL = "http://127.0.0.1:8137/hello";
const RETRY_ALARM = "trace_retry";
const RETRY_PERIOD_MIN = 0.5; // chrome.alarms minimum is ~0.5 min

let paused = false;
let retryQueue = []; // events that failed to POST (collector was down)

chrome.storage.local.get(["paused"], (r) => { paused = !!r.paused; updateBadge(); });

function updateBadge() {
  chrome.action.setBadgeText({ text: paused ? "❚❚" : "REC" });
  chrome.action.setBadgeBackgroundColor({ color: paused ? "#888888" : "#cc2222" });
}

chrome.action.onClicked.addListener(() => {
  paused = !paused;
  chrome.storage.local.set({ paused });
  updateBadge();
  if (!paused) hello();
});

// POST a batch (plus any previously-failed events) to the collector. Returns
// true on success; on failure, requeues for the retry alarm.
async function postEvents(events) {
  if (retryQueue.length) { events = retryQueue.concat(events); retryQueue = []; }
  if (events.length === 0) return true;
  try {
    await fetch(COLLECTOR_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events }),
    });
    return true;
  } catch (e) {
    retryQueue = events.concat(retryQueue).slice(-2000); // bounded
    return false;
  }
}

// Lightweight presence ping; lets the collector mark the extension connected
// without writing anything to the raw stream.
async function hello() {
  try { await fetch(HELLO_URL, { method: "POST" }); } catch (e) { /* collector down */ }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "trace_is_paused") {
    sendResponse({ paused });
    return false;
  }
  if (msg.type === "trace_event") {
    if (paused) { sendResponse({ ok: false, paused: true }); return false; }
    // Returning true + an async sendResponse keeps the worker alive until the
    // POST settles, so the event is actually delivered (not dropped on sleep).
    postEvents([msg.event]).then((ok) => sendResponse({ ok })).catch(() => sendResponse({ ok: false }));
    return true;
  }
  return false;
});

// Retry alarm: survives worker termination; drains anything that failed earlier.
chrome.alarms.create(RETRY_ALARM, { periodInMinutes: RETRY_PERIOD_MIN });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === RETRY_ALARM) { hello(); if (retryQueue.length) postEvents([]); }
});

chrome.runtime.onStartup.addListener(() => { updateBadge(); hello(); });
chrome.runtime.onInstalled.addListener(() => { updateBadge(); hello(); });
updateBadge();
hello();
