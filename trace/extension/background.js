// Trace capture extension — background service worker (Manifest V3).
//
// Responsibilities:
//  - relay normalized raw events from content scripts to the local collector
//    (http://127.0.0.1:8137/event), buffering + retrying if it is briefly down;
//  - own the pause/resume toggle (toolbar action) and expose it to content
//    scripts, so the worker can stop capture with one obvious click (Section 5a).
//
// It does NOT inspect or transform payloads beyond batching; redaction is the
// content script's first line and the collector/RawWriter's enforced backstop.

const COLLECTOR_URL = "http://127.0.0.1:8137/event";
const FLUSH_MS = 500;

let paused = false;
let queue = [];

chrome.storage.local.get(["paused"], (r) => { paused = !!r.paused; updateBadge(); });

function updateBadge() {
  chrome.action.setBadgeText({ text: paused ? "❚❚" : "REC" });
  chrome.action.setBadgeBackgroundColor({ color: paused ? "#888888" : "#cc2222" });
}

chrome.action.onClicked.addListener(() => {
  paused = !paused;
  chrome.storage.local.set({ paused });
  updateBadge();
});

// Content scripts ask whether capture is paused, and push events.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "trace_is_paused") {
    sendResponse({ paused });
    return true;
  }
  if (msg.type === "trace_event" && !paused) {
    queue.push(msg.event);
  }
  return false;
});

async function flush() {
  if (paused || queue.length === 0) return;
  const batch = queue;
  queue = [];
  try {
    await fetch(COLLECTOR_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: batch }),
    });
  } catch (e) {
    // collector not up yet — requeue (bounded) and try again next tick.
    queue = batch.concat(queue).slice(-2000);
  }
}

setInterval(flush, FLUSH_MS);
updateBadge();
