"""Review 2 — environment capture contract (`environment_<id>.json`).

For each approved workflow, derive (from its own event evidence) which real work
products the build will need, gather them by upload, extract their structure, and
ask a minimal set of derived questions for facts no artifact contains. The Artifact
Engine reads this alongside the reviewed bundle.

General by construction (R2/R7): the request set is derived from event actions, not
a per-workflow-type list, and extraction is keyed on artifact kind, not workflow.
Nothing run-specific (field names, columns, formats, action sequences) is hardcoded
— those are runtime data supplied by uploads and answers.
"""

import os
import re

from . import extract

# Actions that read from / write to an external artifact (controlled-vocab, generic).
_READ_ACTIONS = {"open_document"}
_WRITE_ACTIONS = {"enter_data", "update_data"}

VALID_ROLE = {"input", "output", "reference"}
VALID_STATUS = {"complete", "incomplete"}


def event_index(event_log):
    return {e.get("id"): e for e in event_log if isinstance(e, dict) and e.get("id")}


def _kind_guess_from_title(title):
    """Guess artifact kind from a file extension in the window title, else unknown.
    Generic format hint only — the true kind is set from the uploaded file."""
    m = re.search(r"\.([A-Za-z0-9]{1,5})\b", title or "")
    if not m:
        return "unknown"
    return extract.detect_kind("x." + m.group(1).lower())


# --------------------------------------------------------------------------
# Request derivation (R2) — from a reviewed bundle's events
# --------------------------------------------------------------------------
def derive_requests(bundle, idx):
    """Return the list of artifact requests for one reviewed bundle.

    Each artifact-touching action contributes to a request, grouped by (role, app)
    so repeated instances of the same artifact collapse to ONE request asking for a
    single real example. Role comes from the action (read -> input, write ->
    output); kind is a hint from the title (resolved on upload)."""
    groups = {}  # (role, app) -> {role, app, kind, event_ids, titles}
    for eid in bundle.get("evidence_event_ids", []):
        e = idx.get(eid)
        if not e:
            continue
        action = e.get("action", "")
        if action in _WRITE_ACTIONS:
            role = "output"
        elif action in _READ_ACTIONS:
            role = "input"
        else:
            continue
        app = e.get("app", "") or ""
        title = e.get("window", "") or ""
        g = groups.setdefault((role, app), {"role": role, "app": app, "kind": "unknown",
                                            "event_ids": [], "titles": []})
        g["event_ids"].append(eid)
        if title:
            g["titles"].append(title)
        if g["kind"] == "unknown":
            g["kind"] = _kind_guess_from_title(title)

    requests = []
    for g in groups.values():
        sample_title = g["titles"][0] if g["titles"] else g["app"]
        verb = {"input": "read", "output": "write to", "reference": "consult"}[g["role"]]
        kind_word = g["kind"] if g["kind"] != "unknown" else "file"
        requests.append({
            "role": g["role"],
            "kind": g["kind"],
            "derived_from_event_ids": g["event_ids"],
            "label_shown_to_user": (f"Upload one real example of the {kind_word} you {verb} "
                                    f"in {g['app']} (e.g. “{sample_title}”), and say in one line "
                                    f"what it is / how to use it (name the tab or section if relevant)."),
            "user_instruction": "",
            "uploaded": False,
            "file_ref": None,
            "extraction_status": None,
            "extracted": None,
        })
    # stable order: inputs, then outputs, then references
    order = {"input": 0, "output": 1, "reference": 2}
    requests.sort(key=lambda r: (order.get(r["role"], 9), r["kind"]))
    return requests


# --------------------------------------------------------------------------
# Question derivation (R4) — from the extracted (uploaded) artifacts
# --------------------------------------------------------------------------
def _artifact_fields(a):
    """Field/column names an artifact exposes (spreadsheet headers or doc/pdf field
    labels), lowercased for comparison."""
    ex = a.get("extracted") or {}
    fields = [str(h) for h in ex.get("headers", []) if str(h).strip()]
    fields += [f.get("label", "") for f in ex.get("field_layout", []) if f.get("label")]
    return fields


def derive_questions(artifacts):
    """Derive a minimal set of free-text questions from the uploaded artifacts:
    output fields the source doesn't supply, and which sheet is canonical when an
    output has multiple tabs. Field/sheet names are runtime data, never hardcoded."""
    questions = []
    inputs = [a for a in artifacts if a.get("role") in ("input", "reference") and a.get("uploaded")]
    outputs = [a for a in artifacts if a.get("role") == "output" and a.get("uploaded")]

    input_fields = set()
    for a in inputs:
        input_fields.update(f.lower() for f in _artifact_fields(a))

    seen = set()
    for a in outputs:
        for col in _artifact_fields(a):
            key = col.lower()
            if key and key not in input_fields and key not in seen:
                seen.add(key)
                questions.append(f"The output field “{col}” is not supplied by any uploaded "
                                 f"source — how should it be filled?")
        tabs = [t for t in (a.get("extracted") or {}).get("tab_names", [])
                if t and not t.startswith("(")]
        if len(tabs) > 1:
            q = f"The output has multiple sheets ({', '.join(tabs)}) — which is the canonical one?"
            if q not in [x for x in questions]:
                questions.append(q)
    return questions[:12]


# --------------------------------------------------------------------------
# Seed / validate / status
# --------------------------------------------------------------------------
def seed_environment(bundle, event_log):
    """Build the initial environment record for an approved bundle (requests derived,
    nothing uploaded yet)."""
    idx = event_index(event_log)
    return {
        "bundle_id": bundle.get("id"),
        "artifacts": derive_requests(bundle, idx),
        "answers": [],
        "status": "incomplete",
    }


def compute_status(record):
    """complete iff every requested artifact is uploaded AND has a user instruction.

    Auto-extraction is non-load-bearing (R5): a failed/empty extraction with the raw
    file and instruction present is COMPLETE. ``environment-incomplete`` is reserved
    for a missing required upload or a missing instruction."""
    arts = record.get("artifacts", [])
    for a in arts:
        if not a.get("uploaded"):
            return "incomplete"
        if not str(a.get("user_instruction", "") or "").strip():
            return "incomplete"
    return "complete"


def _extraction_status(extracted):
    """Classify a best-effort extraction result. Never authoritative."""
    if not isinstance(extracted, dict):
        return "failed"
    has_content = bool(extracted.get("headers") or extracted.get("sample_rows")
                       or str(extracted.get("text", "")).strip() or extracted.get("field_layout"))
    if not has_content:
        return "failed"
    return "partial" if extracted.get("note") else "ok"


def _norm_artifact(a):
    role = a.get("role") if a.get("role") in VALID_ROLE else "input"
    extracted = a.get("extracted") if isinstance(a.get("extracted"), dict) else None
    es = a.get("extraction_status")
    if es not in ("ok", "partial", "failed"):
        es = _extraction_status(extracted) if a.get("uploaded") else None
    return {
        "role": role,
        "kind": str(a.get("kind", "unknown") or "unknown"),
        "derived_from_event_ids": [str(x) for x in (a.get("derived_from_event_ids") or [])],
        "label_shown_to_user": str(a.get("label_shown_to_user", "") or ""),
        "user_instruction": str(a.get("user_instruction", "") or ""),
        "uploaded": bool(a.get("uploaded")),
        "file_ref": a.get("file_ref"),
        "extraction_status": es,
        "extracted": extracted,
    }


def validate_environment(obj):
    """Validate/repair an environment record to the frozen shape. Never raises."""
    if not isinstance(obj, dict):
        obj = {}
    arts = [_norm_artifact(a) for a in (obj.get("artifacts") or []) if isinstance(a, dict)]
    answers = []
    for q in (obj.get("answers") or []):
        if isinstance(q, dict) and q.get("question"):
            answers.append({"question": str(q["question"]), "answer": str(q.get("answer", "") or "")})
    rec = {"bundle_id": obj.get("bundle_id"), "artifacts": arts, "answers": answers,
           "status": obj.get("status") if obj.get("status") in VALID_STATUS else "incomplete"}
    rec["status"] = compute_status(rec)
    return rec


def attach_upload(record, artifact_index, file_path, original_filename):
    """Record an uploaded file against a request: detect kind, extract structure,
    refresh derived questions and status. Returns the updated record."""
    arts = record.get("artifacts", [])
    if not (0 <= artifact_index < len(arts)):
        return record
    with open(file_path, "rb") as f:
        head = f.read(8)
    kind = extract.detect_kind(original_filename, head)
    if kind == "unknown" and arts[artifact_index].get("kind") in (None, "unknown"):
        kind = arts[artifact_index].get("kind") or "unknown"
    elif kind == "unknown":
        kind = arts[artifact_index]["kind"]
    # Best-effort, non-load-bearing extraction (R3): never raises, never overrides
    # the user instruction, never the sole basis for an assertion.
    extracted = extract.extract(kind, file_path)
    arts[artifact_index].update({
        "uploaded": True,
        "file_ref": file_path,                       # raw file always travels (R2)
        "kind": kind if kind != "unknown" else arts[artifact_index]["kind"],
        "extraction_status": _extraction_status(extracted),
        "extracted": extracted,
        # user_instruction is intentionally NOT touched here (set via the UI/save)
    })
    # re-derive advisory questions from the new artifact set, preserving answers
    prior = {q["question"]: q["answer"] for q in record.get("answers", [])}
    record["answers"] = [{"question": q, "answer": prior.get(q, "")}
                         for q in derive_questions(arts)]
    record["status"] = compute_status(record)
    return record


# --------------------------------------------------------------------------
# Rendering for the engine (folded into the spec prompt + assembly)
# --------------------------------------------------------------------------
def artifact_basename(a):
    ref = a.get("file_ref") or ""
    return os.path.basename(ref) if ref else (a.get("label_shown_to_user") or a.get("kind") or "file")


def render_artifact_for_prompt(a):
    """Render an artifact for the model: the user instruction is AUTHORITATIVE; the
    raw file is always referenced; auto-extraction appears only as a clearly-labeled,
    unverified aid (R1–R3)."""
    lines = [f'- {a["role"].upper()} artifact — raw file: {artifact_basename(a)}'
             f' (path: {a.get("file_ref")})']
    instr = (a.get("user_instruction") or "").strip()
    lines.append(f'    USER INSTRUCTION (authoritative): {instr or "(none given)"}')
    ex = a.get("extracted") or {}
    es = a.get("extraction_status")
    if ex and es in ("ok", "partial"):
        bits = [f'format={ex.get("format", "?")}']
        if ex.get("tab_names"):
            bits.append("sheets=" + ", ".join(str(t) for t in ex["tab_names"]))
        if ex.get("headers"):
            bits.append("columns=" + ", ".join(str(h) for h in ex["headers"]))
        if ex.get("field_layout"):
            bits.append("fields=" + ", ".join(f.get("label", "") for f in ex["field_layout"][:12]))
        if ex.get("text"):
            bits.append("text=" + ex["text"][:200])
        lines.append(f'    auto-read (UNVERIFIED, best-effort — do not treat as ground truth): '
                     f'{" | ".join(bits)}')
    else:
        lines.append(f'    auto-read: {es or "none"} (rely on the raw file + instruction, not extraction)')
    return "\n".join(lines)


def instruction_criteria(record):
    """Deterministic acceptance criteria that target the USER-DESIGNATED artifact and
    location and are checkable LIVE against the real uploaded file (R4). They never
    assert against an auto-extracted schema/location; extraction is not ground truth.
    """
    crit = []
    for a in record.get("artifacts", []):
        if not a.get("uploaded"):
            continue
        instr = (a.get("user_instruction") or "").strip()
        name = artifact_basename(a)
        if instr:
            crit.append(f'The agent must use the {a["role"]} artifact «{name}» exactly as the user '
                        f'instructed — "{instr}" — reading the real uploaded file live at build/test '
                        f'time (do not rely on any auto-extracted summary).')
        else:
            crit.append(f'The agent must read the {a["role"]} artifact «{name}» live from the real '
                        f'uploaded file (its instruction was not provided).')
    for q in record.get("answers", []):
        if str(q.get("answer", "")).strip():
            crit.append(f"{q['question']} → {q['answer']}")
    return crit
