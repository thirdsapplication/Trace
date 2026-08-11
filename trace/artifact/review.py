"""The review-step contract (`reviewed_bundles.json`) and helpers.

The review step (in the Test UI) lets the operator lightly correct what discovery
surfaced before any spec is built. Its output is the frozen ``reviewed_bundles.json``
contract that the Artifact Engine reads — never raw ``discovery.json``.

This module is the shared substrate for both the UI (which seeds the review state
from discovery, fetches verbatim examples, and saves edits) and the Artifact Engine
(which consumes the saved contract). It contains no business/fixture/answer-key
content; the only real-world content is the user's own event log and edits.
"""

VALID_CLASSIFICATION = {"bundle", "anti_bundle"}
VALID_DISPOSITION = {"approve_for_build", "reject"}
VALID_CONFIDENCE = {"high", "medium", "low"}

# verbatim event fields carried into an observed example (never paraphrased)
_EXAMPLE_FIELDS = ("id", "ts_start", "ts_end", "app", "window", "action",
                   "detail", "confidence")


class ReviewReport:
    def __init__(self):
        self.errors = []
        self.flags = []

    def error(self, m):
        self.errors.append(m)

    def flag(self, m):
        self.flags.append(m)

    def __str__(self):
        out = []
        if self.errors:
            out += [f"{len(self.errors)} repair(s):"] + [f"  - {e}" for e in self.errors]
        if self.flags:
            out += [f"{len(self.flags)} flag(s):"] + [f"  - {f}" for f in self.flags]
        return "\n".join(out) if out else "clean"


def event_index(event_log):
    """Map event id -> event dict for verbatim example lookup."""
    return {e.get("id"): e for e in event_log if isinstance(e, dict) and e.get("id")}


def fetch_examples(event_log, event_ids):
    """Return the verbatim observed examples for the given ids, in log order.
    Examples are copied field-for-field from the event log — never edited."""
    idx = event_index(event_log)
    out = []
    for eid in event_ids:
        e = idx.get(eid)
        if e:
            out.append({k: e.get(k) for k in _EXAMPLE_FIELDS})
    return out


def _as_list(v):
    if isinstance(v, list):
        return v
    return [] if v in (None, "") else [v]


def seed_review(discovery):
    """Build the default ``reviewed_bundles`` structure from a ``discovery`` dict:
    every item keeps discovery's classification, all examples included, no
    checkpoints, no added criteria, bundles defaulted to approve_for_build."""
    bundles = []
    for b in discovery.get("bundles", []) or []:
        ev = list(b.get("evidence_event_ids", []) or [])
        bundles.append({
            "id": b.get("id"),
            "classification": "bundle",
            "name": b.get("name", ""),
            "description": b.get("description", ""),
            "trigger": b.get("trigger", ""),
            "steps": list(b.get("steps", []) or []),
            "disposition": "approve_for_build",
            "human_checkpoints": [],
            "included_example_event_ids": list(ev),
            "added_acceptance_criteria": [],
            "preferred_output": "",
            "evidence_event_ids": ev,
            "confidence": b.get("confidence", "medium"),
            # one-line, user-facing reason for the classification (Spec 5); fall back
            # to the engine's internal "why" if an older discovery lacks rationale
            "rationale": (b.get("rationale") or b.get("why_automatable") or "").strip(),
            "reclassified_from": None,
            "original_reason_for_judgment": "",
            "user_description": "",
            "understanding": "",
        })
    anti = []
    for a in discovery.get("anti_bundles", []) or []:
        ev = list(a.get("evidence_event_ids", []) or [])
        anti.append({
            "id": a.get("id"),
            "classification": "anti_bundle",
            "name": a.get("name", ""),
            "description": a.get("description", ""),
            "why_judgment": a.get("why_judgment", ""),
            "recommended_handling": a.get("recommended_handling", ""),
            "rationale": (a.get("rationale") or a.get("why_judgment") or "").strip(),
            "included_example_event_ids": list(ev),
            "evidence_event_ids": ev,
            "confidence": a.get("confidence", "medium"),
            "reclassified_from": None,
            "user_description": "",
            "understanding": "",
        })
    return {"bundles": bundles, "anti_bundles": anti,
            "notes": discovery.get("notes", "")}


def _clean_ids(ids, valid, report, where):
    out, seen = [], set()
    for i in _as_list(ids):
        i = str(i).strip()
        if i in valid and i not in seen:
            seen.add(i)
            out.append(i)
        elif i and i not in valid:
            report.error(f"{where} referenced unknown event id {i!r} (dropped)")
    return out


def _norm_checkpoints(raw, n_steps, report, where):
    out = []
    for c in _as_list(raw):
        if not isinstance(c, dict):
            continue
        try:
            idx = int(c.get("step_index"))
        except (TypeError, ValueError):
            report.error(f"{where} checkpoint had a non-integer step_index (dropped)")
            continue
        if not (0 <= idx < n_steps):
            report.flag(f"{where} checkpoint step_index {idx} out of range (kept, clamped)")
            idx = max(0, min(idx, n_steps - 1)) if n_steps else 0
        out.append({"step_index": idx, "note": str(c.get("note", "") or "").strip()})
    return out


def _norm_conf(c):
    return c if c in VALID_CONFIDENCE else "medium"


def _norm_understanding(raw, report, where):
    """The review step's approve/correct marker. A non-empty correction is the
    signal: it forces ``understanding="corrected"``; ``"corrected"`` with no text
    is repaired to unresolved. The correction text is kept VERBATIM (stripped
    only) — it is the worker's authoritative description of the workflow."""
    ud = str(raw.get("user_description", "") or "").strip()
    und = raw.get("understanding")
    if ud:
        und = "corrected"
    elif und == "corrected":
        report.error(f"{where} marked corrected but had no correction text (cleared)")
        und = ""
    if und not in ("confirmed", "corrected"):
        und = ""
    if len(ud) > 2000:
        report.flag(f"{where} correction is very long ({len(ud)} chars); kept verbatim")
    return ud, und


def validate_reviewed(obj, valid_event_ids, report):
    """Validate/repair a ``reviewed_bundles`` object. Items are routed to the right
    list by their ``classification`` (so reclassification in either direction is
    honored), required fields are filled, and event-id references are filtered to
    ids that exist in the event log. Always returns a contract-shaped dict."""
    if not isinstance(obj, dict):
        report.error("reviewed_bundles was not an object")
        return {"bundles": [], "anti_bundles": [], "notes": ""}

    items = list(_as_list(obj.get("bundles"))) + list(_as_list(obj.get("anti_bundles")))
    bundles, anti = [], []
    for raw in items:
        if not isinstance(raw, dict):
            report.error("dropped a non-object reviewed item")
            continue
        cls = raw.get("classification")
        if cls not in VALID_CLASSIFICATION:
            # infer from shape: presence of steps/disposition -> bundle
            cls = "bundle" if (raw.get("steps") or raw.get("disposition")) else "anti_bundle"
        where = f"{raw.get('id', '?')}"
        ev = _clean_ids(raw.get("evidence_event_ids"), valid_event_ids, report, where)
        incl = _clean_ids(raw.get("included_example_event_ids"), valid_event_ids, report, where)
        ud, und = _norm_understanding(raw, report, where)

        if cls == "bundle":
            steps = [str(s) for s in _as_list(raw.get("steps"))]
            disp = raw.get("disposition")
            if disp not in VALID_DISPOSITION:
                report.error(f"{where} had invalid disposition {disp!r} -> reject")
                disp = "reject"
            recl = raw.get("reclassified_from")
            if recl == "anti_bundle" and not steps and disp == "approve_for_build":
                report.flag(f"{where} reclassified anti->bundle but has no steps to build")
            bundles.append({
                "id": raw.get("id"),
                "classification": "bundle",
                "name": str(raw.get("name", "") or "").strip(),
                "description": str(raw.get("description", "") or "").strip(),
                "trigger": str(raw.get("trigger", "") or "").strip(),
                "steps": steps,
                "disposition": disp,
                "human_checkpoints": _norm_checkpoints(raw.get("human_checkpoints"),
                                                       len(steps), report, where),
                "included_example_event_ids": incl,
                "added_acceptance_criteria": [str(c).strip() for c in
                                              _as_list(raw.get("added_acceptance_criteria")) if str(c).strip()],
                "preferred_output": str(raw.get("preferred_output", "") or "").strip(),
                "evidence_event_ids": ev,
                "confidence": _norm_conf(raw.get("confidence")),
                "rationale": str(raw.get("rationale", "") or "").strip(),
                "reclassified_from": recl if recl in VALID_CLASSIFICATION else None,
                "original_reason_for_judgment": str(raw.get("original_reason_for_judgment", "") or "").strip(),
                "user_description": ud,
                "understanding": und,
            })
        else:
            anti.append({
                "id": raw.get("id"),
                "classification": "anti_bundle",
                "name": str(raw.get("name", "") or "").strip(),
                "description": str(raw.get("description", "") or "").strip(),
                "why_judgment": str(raw.get("why_judgment", "") or "").strip(),
                "recommended_handling": str(raw.get("recommended_handling", "") or "").strip(),
                "rationale": str(raw.get("rationale", "") or "").strip(),
                "included_example_event_ids": incl,
                "evidence_event_ids": ev,
                "confidence": _norm_conf(raw.get("confidence")),
                "reclassified_from": (raw.get("reclassified_from")
                                      if raw.get("reclassified_from") in VALID_CLASSIFICATION else None),
                "user_description": ud,
                "understanding": und,
            })

    return {"bundles": bundles, "anti_bundles": anti,
            "notes": str(obj.get("notes", "") or "")}
