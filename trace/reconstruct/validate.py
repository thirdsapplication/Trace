"""Validate, normalize, and stitch reconstructed event-log output.

Three jobs:
1. **Schema validation** against the frozen ``event_log.json`` contract
   (schema/SCHEMA.md). Coerce/repair where safe, flag where not — never crash on
   a model slip.
2. **Action normalization** to the small controlled vocabulary, via a generic,
   domain-neutral synonym map, so the same real task yields a comparable action
   sequence (Engine 2 recurrence depends on this).
3. **Cross-chunk stitching + fragment coherence.** Dedupe events produced twice in
   the overlap region, renumber ids, backfill ts/app/window from the cited raw
   evidence, and flag (do not drop) typed values that look fragmented/garbled so a
   human can review them (handoff Section 5b.5).

Flags are reported out-of-band (a ValidationReport), never written into the
event-log objects, so ``event_log.json`` stays exactly on the contract shape.
"""

import re

from .prompt import ACTION_VOCAB

VALID_CONFIDENCE = {"high", "medium", "low"}
_VOCAB = set(ACTION_VOCAB)

# Domain-neutral synonym map -> canonical vocabulary. No business verbs.
_SYNONYMS = {
    "read_mail": "read_email",
    "view_email": "read_email",
    "open_email": "read_email",
    "reply_to_email": "reply_email",
    "respond_email": "reply_email",
    "send_mail": "send_email",
    "write_email": "compose_email",
    "draft_email": "compose_email",
    "open_pdf": "view_pdf",
    "read_pdf": "view_pdf",
    "open_file": "open_document",
    "open_doc": "open_document",
    "read_doc": "read_document",
    "enter_order_data": "enter_data",
    "enter_order": "enter_data",
    "input_data": "enter_data",
    "add_row": "enter_data",
    "update_pricing": "update_data",
    "edit_data": "update_data",
    "update_cell": "update_data",
    "copy": "copy_value",
    "copy_text": "copy_value",
    "paste": "paste_value",
    "paste_text": "paste_value",
    "find": "search",
    "lookup": "search",
    "look_up": "search",
    "goto": "navigate",
    "go_to": "navigate",
    "open_url": "navigate",
    "choose_option": "select_option",
    "select_dropdown": "select_option",
    "click_button": "click_control",
    "click": "click_control",
    "switch_app": "switch_window",
    "switch_tab": "switch_window",
}


class ValidationReport:
    def __init__(self):
        self.errors = []    # structural problems repaired or dropped
        self.flags = []     # things a human should eyeball (fragmentation, etc.)

    def error(self, msg):
        self.errors.append(msg)

    def flag(self, msg):
        self.flags.append(msg)

    def __str__(self):
        out = []
        if self.errors:
            out.append(f"{len(self.errors)} repair(s):")
            out += [f"  - {e}" for e in self.errors]
        if self.flags:
            out.append(f"{len(self.flags)} review flag(s):")
            out += [f"  - {f}" for f in self.flags]
        return "\n".join(out) if out else "clean"


def normalize_action(action):
    """Lowercase, snake_case, and map to the controlled vocabulary where possible.
    Returns ``(normalized, was_in_vocab)``."""
    if not isinstance(action, str) or not action.strip():
        return "unknown", False
    a = action.strip().lower()
    a = re.sub(r"[\s\-]+", "_", a)
    a = re.sub(r"[^a-z0-9_]", "", a)
    a = re.sub(r"_+", "_", a).strip("_")
    if not a:
        return "unknown", False
    a = _SYNONYMS.get(a, a)
    return a, (a in _VOCAB)


def _alnum(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def validate_event(ev, raw_by_id, report):
    """Validate/repair one model-produced event against the contract. Returns the
    cleaned event dict, or ``None`` if it is unsalvageable."""
    if not isinstance(ev, dict):
        report.error("dropped a non-object event")
        return None

    detail = ev.get("detail")
    if not isinstance(detail, str) or not detail.strip():
        report.error("dropped event with empty detail")
        return None

    # evidence -> list of known raw ids
    evidence = ev.get("evidence", [])
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []
    clean_ev = []
    for e in evidence:
        e = str(e).strip()
        m = re.search(r"r\d{1,}", e)
        rid = m.group(0) if m else e
        rid = _normalize_rid(rid, raw_by_id)
        if rid in raw_by_id:
            clean_ev.append(rid)
        else:
            report.error(f'event cited unknown evidence id "{e}"')
    # dedupe preserving order
    seen = set()
    clean_ev = [x for x in clean_ev if not (x in seen or seen.add(x))]

    action, in_vocab = normalize_action(ev.get("action"))
    if not in_vocab:
        report.flag(f'action "{action}" is outside the controlled vocabulary')

    confidence = ev.get("confidence")
    if confidence not in VALID_CONFIDENCE:
        report.error(f'invalid confidence "{confidence}" -> defaulted to low')
        confidence = "low"

    # backfill ts/app/window from cited evidence where missing
    ts_start = ev.get("ts_start") or ""
    ts_end = ev.get("ts_end") or ""
    app = ev.get("app") or ""
    window = ev.get("window") or ""
    cited = [raw_by_id[r] for r in clean_ev]
    if cited:
        ts_vals = [c["ts"] for c in cited if c.get("ts")]
        if ts_vals:
            ts_start = ts_start or min(ts_vals)
            ts_end = ts_end or max(ts_vals)
        win = next((c for c in cited if c["source"] == "window"), None)
        if win:
            app = app or win["payload"].get("app", "")
            window = window or win["payload"].get("title", "")
        else:
            ks = next((c for c in cited if c["source"] == "keystroke"), None)
            if ks:
                window = window or ks["payload"].get("window", "")

    cleaned = {
        "id": ev.get("id", ""),
        "ts_start": ts_start,
        "ts_end": ts_end,
        "app": app,
        "window": window,
        "action": action,
        "detail": detail.strip(),
        "evidence": clean_ev,
        "confidence": confidence,
    }
    _check_fragmentation(cleaned, raw_by_id, report)
    return cleaned


def _normalize_rid(rid, raw_by_id):
    """Map a bare/short id like 'r7' to the zero-padded 'r0007' if that exists."""
    if rid in raw_by_id:
        return rid
    m = re.fullmatch(r"r(\d+)", rid)
    if m:
        padded = f"r{int(m.group(1)):04d}"
        if padded in raw_by_id:
            return padded
    return rid


def _check_fragmentation(ev, raw_by_id, report):
    """Flag (not drop) events whose cited typed text seems to have been lost in
    stitching: if the event cites keystroke evidence but little of that typed text
    survived into the detail, a value may have been dropped or garbled."""
    typed = [raw_by_id[r]["payload"].get("text", "")
             for r in ev["evidence"] if raw_by_id[r]["source"] == "keystroke"]
    if len(typed) < 2:
        return
    detail_alnum = _alnum(ev["detail"])
    missing = 0
    significant = 0
    for frag in typed:
        for tok in re.split(r"[\s]+", frag):
            tok_a = _alnum(tok)
            if len(tok_a) < 3:
                continue
            significant += 1
            if tok_a not in detail_alnum:
                missing += 1
    if significant >= 3 and missing > significant / 2:
        report.flag(
            f'event {ev.get("id","?")} ("{ev["action"]}") cites {len(typed)} typed '
            f'fragments but ~{missing}/{significant} significant tokens are absent '
            f"from its detail — possible fragmented/garbled value, review."
        )


def stitch(chunk_outputs, raw_by_id, report):
    """Merge per-chunk event lists into one log, deduping the overlap region and
    renumbering ids. An event whose evidence is fully contained in evidence already
    claimed by accepted events is treated as an overlap duplicate and skipped."""
    accepted = []
    claimed = set()
    for events in chunk_outputs:
        for ev in events:
            cleaned = validate_event(ev, raw_by_id, report)
            if cleaned is None:
                continue
            ev_ids = set(cleaned["evidence"])
            if ev_ids and ev_ids <= claimed:
                # fully within already-covered raw evidence -> overlap duplicate
                continue
            accepted.append(cleaned)
            claimed |= ev_ids

    # order by ts_start (fallback: keep arrival order via stable sort)
    accepted.sort(key=lambda e: (e.get("ts_start") or "", ))
    for i, ev in enumerate(accepted, start=1):
        ev["id"] = f"e{i:03d}"
    return accepted
