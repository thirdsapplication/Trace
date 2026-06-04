// Trace capture extension — content script.
//
// Captures the browser-only signals the lean v1 stream needs, all event-anchored
// (never on a timer) and scoped to the unique delta — never a full-page DOM dump,
// which would just recreate the OCR redundancy/cost v1 deliberately removed:
//
//   click            : a mouse click + its target element {role,label,value}
//   target           : a focus change + the focused element {role,label,value}
//   clipboard        : copy / cut / paste with content
//   structured_read  : the *delta* of visible labels/values/options at an anchor
//                      (page_load | click | focus)
//
// Privacy (Section 5a):
//   - input[type=password] (and autocomplete=*-password, contenteditable marked
//     sensitive) are NEVER read — not their value, not into structured reads.
//   - obvious secrets are redacted client-side too; the collector redacts again.

(function () {
  "use strict";

  // ---- pause gate -------------------------------------------------------
  let paused = false;
  function refreshPaused() {
    try {
      chrome.runtime.sendMessage({ type: "trace_is_paused" }, (r) => {
        if (chrome.runtime.lastError) return;
        if (r) paused = !!r.paused;
      });
    } catch (e) { /* worker asleep; default to capturing */ }
  }
  refreshPaused();
  setInterval(refreshPaused, 2000);

  function send(source, payload) {
    if (paused) return;
    try {
      chrome.runtime.sendMessage({
        type: "trace_event",
        event: { ts: new Date().toISOString(), source, payload },
      });
    } catch (e) { /* ignore: worker may be restarting */ }
  }

  // ---- password / sensitivity detection ---------------------------------
  function isSensitive(el) {
    if (!el) return false;
    if (el.tagName === "INPUT") {
      const t = (el.getAttribute("type") || "").toLowerCase();
      if (t === "password") return true;
      const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
      if (ac.includes("password") || ac === "one-time-code" || ac === "cc-number") return true;
    }
    if (el.getAttribute && el.getAttribute("data-trace-sensitive") != null) return true;
    return false;
  }

  // ---- light client-side redaction (collector enforces the backstop) ----
  function redact(s) {
    if (!s || typeof s !== "string") return s;
    return s
      .replace(/\b(?:\d[ -]?){13,19}\b/g, (m) => (luhn(m) ? "[REDACTED]" : m))
      .replace(/\b\d{3}[ -]\d{2}[ -]\d{4}\b/g, "[REDACTED]")
      .replace(/\bsk-[A-Za-z0-9_\-]{16,}\b/g, "[REDACTED]")
      .replace(/\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+/g, "[REDACTED]")
      .replace(/\b(?:password|secret|token|api[_-]?key)\b\s*[:=]\s*\S+/gi, "[REDACTED]");
  }
  function luhn(str) {
    const d = (str.match(/\d/g) || []).map(Number);
    if (d.length < 13 || d.length > 19) return false;
    let sum = 0, par = d.length % 2;
    d.forEach((n, i) => { if (i % 2 === par) { n *= 2; if (n > 9) n -= 9; } sum += n; });
    return sum % 10 === 0;
  }

  // ---- element description ----------------------------------------------
  function roleOf(el) {
    const explicit = el.getAttribute && el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName ? el.tagName.toLowerCase() : "";
    switch (tag) {
      case "a": return "link";
      case "button": return "button";
      case "select": return "combobox";
      case "textarea": return "textbox";
      case "input": {
        const t = (el.getAttribute("type") || "text").toLowerCase();
        if (["button", "submit", "reset"].includes(t)) return "button";
        if (t === "checkbox") return "checkbox";
        if (t === "radio") return "radio";
        return "textbox";
      }
      default: return tag || "element";
    }
  }

  function labelOf(el) {
    const aria = el.getAttribute && el.getAttribute("aria-label");
    if (aria) return aria.trim();
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && lab.textContent) return lab.textContent.trim();
    }
    const wrapLabel = el.closest && el.closest("label");
    if (wrapLabel && wrapLabel.textContent) return wrapLabel.textContent.trim();
    const ph = el.getAttribute && el.getAttribute("placeholder");
    if (ph) return ph.trim();
    const name = el.getAttribute && el.getAttribute("name");
    if (name) return name.trim();
    const txt = (el.textContent || "").trim();
    if (txt && txt.length <= 80) return txt;
    return "";
  }

  function valueOf(el) {
    if (isSensitive(el)) return "[REDACTED]";
    const tag = el.tagName ? el.tagName.toLowerCase() : "";
    if (tag === "select") {
      const opt = el.options && el.options[el.selectedIndex];
      return redact(opt ? opt.textContent.trim() : (el.value || ""));
    }
    if (tag === "input" || tag === "textarea") {
      // keystrokes already carry typed text; here we capture mouse-chosen values
      // (checkbox/radio/date pickers/etc.) so they aren't invisible.
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "checkbox" || t === "radio") return el.checked ? "checked" : "unchecked";
      return redact(el.value || "");
    }
    return "";
  }

  function describe(el) {
    if (!el || !el.tagName) return null;
    return {
      role: roleOf(el),
      label: redact(labelOf(el)),
      value: valueOf(el),
    };
  }

  // ---- scoped structured-read delta -------------------------------------
  // Track {label::value} signatures already reported on this page so each read
  // emits only what changed. Reset on navigation (page_load).
  let seen = new Set();

  function sig(e) { return `${e.role}|${e.label}|${e.value}`; }

  function structuredDelta(anchorEl) {
    const out = [];
    // Form controls (non-sensitive, visible) carry field labels + values not
    // already in keystrokes/window title.
    const controls = document.querySelectorAll(
      "input, select, textarea, [role=combobox], [role=listbox], [role=option]"
    );
    controls.forEach((el) => {
      if (isSensitive(el)) return;
      const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
      if (rect && rect.width === 0 && rect.height === 0) return; // hidden
      const d = describe(el);
      if (!d || (!d.label && !d.value)) return;
      const s = sig(d);
      if (seen.has(s)) return;
      seen.add(s);
      out.push(d);
    });
    // If the anchor is a <select>, include its available options (the dropdown
    // choices), once.
    if (anchorEl && anchorEl.tagName === "SELECT") {
      Array.from(anchorEl.options).forEach((o) => {
        const d = { role: "option", label: redact((o.textContent || "").trim()), value: "" };
        const s = sig(d);
        if (!seen.has(s)) { seen.add(s); out.push(d); }
      });
    }
    return out;
  }

  function emitStructuredRead(anchor, anchorEl) {
    const elements = structuredDelta(anchorEl);
    if (elements.length === 0) return; // delta empty -> nothing new to report
    send("structured_read", { url: location.href, anchor, elements });
  }

  // ---- listeners --------------------------------------------------------
  document.addEventListener("click", (ev) => {
    const el = ev.target;
    const button = ev.button === 2 ? "right" : ev.button === 1 ? "middle" : "left";
    send("click", {
      x: ev.clientX, y: ev.clientY, button,
      target: describe(el),
    });
    emitStructuredRead("click", el && el.tagName === "SELECT" ? el : null);
  }, true);

  document.addEventListener("focusin", (ev) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    if (!isSensitive(el)) {
      send("target", describe(el));
    }
    emitStructuredRead("focus", el && el.tagName === "SELECT" ? el : null);
  }, true);

  function clipboardHandler(op) {
    return (ev) => {
      const active = document.activeElement;
      if (isSensitive(active)) return; // never capture clipboard near a password field
      let content = "";
      try {
        if (op === "paste" && ev.clipboardData) {
          content = ev.clipboardData.getData("text/plain");
        } else {
          content = (window.getSelection && window.getSelection().toString()) || "";
        }
      } catch (e) { content = ""; }
      if (!content) return;
      send("clipboard", { op, content: redact(content) });
    };
  }
  document.addEventListener("copy", clipboardHandler("copy"), true);
  document.addEventListener("cut", clipboardHandler("cut"), true);
  document.addEventListener("paste", clipboardHandler("paste"), true);

  // page load anchor
  seen = new Set();
  emitStructuredRead("page_load", null);
})();
