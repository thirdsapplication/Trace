"""The Artifact Engine: reviewed_bundles.json (+ event_log.json) -> spec_<id>.md.

For each reviewed item classified as a ``bundle`` with disposition
``approve_for_build``, one focused model call produces the intent prose, the
example-supported acceptance criteria, and a representative/illustrative assessment
of each observed example; deterministic code then assembles the three-layer Artifact
C markdown spec so that the verbatim examples, the user's added criteria, and a
pause-and-ask criterion per human checkpoint are guaranteed to appear exactly.

- ``bundle`` + ``reject``       -> nothing built.
- ``anti_bundle``               -> a short "handle manually — here's why" note
                                   (deterministic, no model call).
- reclassified anti->bundle     -> built like any bundle, with the carried-over
                                   reason-for-judgment included as context.

Model default: claude-fable-5-1 (single swappable default); ``model_fn`` injectable
so the engine is testable without a key/network. Spec quality matters because it is
what gets built from.
"""

import json
import os
import shutil

from . import environment, review
from .prompt import SYSTEM_PROMPT
from trace.agent import model_call

DEFAULT_MODEL = "claude-fable-5-1"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _default_model_fn(model):
    import anthropic
    client = anthropic.Anthropic()

    def call(system, user):
        return model_call.create_text(client, model, system, user, max_tokens=4000)

    return call


def _parse_json_object(text):
    import re
    if not text:
        return {}
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t) if isinstance(json.loads(t), dict) else {}
    except json.JSONDecodeError:
        s, e = t.find("{"), t.rfind("}")
        if s == -1 or e <= s:
            return {}
        try:
            d = json.loads(t[s:e + 1])
            return d if isinstance(d, dict) else {}
        except json.JSONDecodeError:
            return {}


# --------------------------------------------------------------------------
# Prompt material (also used by --dry-run)
# --------------------------------------------------------------------------
def _render_example(ex):
    loc = f'{ex.get("app", "")} / {ex.get("window", "")}'.strip(" /")
    return (f'[{ex.get("id")}] ({loc}) {ex.get("action", "")} @ {ex.get("ts_start", "")}\n'
            f'    {ex.get("detail", "")}')


def build_prompt_material(bundle, examples, env_record=None):
    """The user-turn content for one bundle (the bundle's edited intent + verbatim
    examples + criteria + checkpoints + any environment artifacts). Shared by the
    real call and --dry-run."""
    steps = bundle.get("steps", [])
    cps = bundle.get("human_checkpoints", [])
    lines = [
        f'WORKFLOW: {bundle.get("name", "")}',
        f'DESCRIPTION: {bundle.get("description", "")}',
        f'TRIGGER: {bundle.get("trigger", "")}',
        "STEPS:",
    ]
    lines += [f'  {i}. {s}' for i, s in enumerate(steps)]
    if bundle.get("preferred_output"):
        lines += ["PREFERRED OUTPUT:", f'  {bundle["preferred_output"]}']
    if bundle.get("reclassified_from") == "anti_bundle" and bundle.get("original_reason_for_judgment"):
        lines += ["ORIGINAL REASON FOR JUDGMENT (reclassified to automatable):",
                  f'  {bundle["original_reason_for_judgment"]}']
    if cps:
        lines += ["HUMAN CHECKPOINTS (steps where the agent must pause and ask):"]
        for c in cps:
            note = f' — {c["note"]}' if c.get("note") else ""
            lines.append(f'  step {c["step_index"]}{note}')
    if bundle.get("added_acceptance_criteria"):
        lines += ["USER-ADDED ACCEPTANCE CRITERIA:"]
        lines += [f'  - {c}' for c in bundle["added_acceptance_criteria"]]
    uploaded = [a for a in (env_record or {}).get("artifacts", []) if a.get("uploaded")]
    if uploaded:
        lines += ["EXTRACTED ENVIRONMENT ARTIFACTS (real uploaded files — primary examples):"]
        lines += [environment.render_artifact_for_prompt(a) for a in uploaded]
        answered = [q for q in (env_record or {}).get("answers", []) if str(q.get("answer", "")).strip()]
        if answered:
            lines += ["ANSWERS TO ENVIRONMENT QUESTIONS:"]
            lines += [f'  - {q["question"]} -> {q["answer"]}' for q in answered]
    lines += ["OBSERVED EXAMPLES from the event log (verbatim — do not quote in your output;"
              " supplementary if environment artifacts are present):"]
    lines += [_render_example(ex) for ex in examples] or ["  (none)"]
    lines += ["", "Produce the JSON object per your instructions."]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Deterministic markdown assembly
# --------------------------------------------------------------------------
def _checkpoint_criterion(cp, steps):
    note = cp.get("note", "").strip()
    if note:
        return f"The agent must pause and request human input when: {note}"
    idx = cp.get("step_index", 0)
    step_txt = steps[idx] if 0 <= idx < len(steps) else f"step {idx}"
    return f"The agent must pause and request human input at the step: {step_txt}"


def assemble_spec(bundle, examples, model_out, env_record=None, plan=None):
    """Assemble the three-layer Artifact C markdown plus a mandatory preflight from
    the (verbatim) examples, the model's intent/criteria/assessments, and any
    environment artifacts. ``plan`` is the export plan (one entry per uploaded
    artifact, with spec-relative ``rel`` paths); the verbatim examples, user-added
    criteria, per-checkpoint criteria, and instruction-based criteria are inserted
    deterministically.

    All artifact references are spec-relative (P2) — no session-storage path appears.
    Observed Examples are led by the real uploaded artifacts when present; event-log
    instances become supplementary. A missing/incomplete environment record flags
    the spec ``environment-incomplete`` rather than silently degrading."""
    name = bundle.get("name") or bundle.get("id")
    intent = (model_out.get("intent") or bundle.get("description") or "").strip()
    assessments = model_out.get("example_assessments") or {}
    derived = [str(c).strip() for c in (model_out.get("derived_acceptance_criteria") or []) if str(c).strip()]
    plan = plan or []
    env_complete = bool(env_record) and env_record.get("status") == "complete"

    md = [f"# Build spec: {name}",
          f"<!-- bundle_id: {bundle.get('id')} | generated by the Trace Artifact Engine -->",
          ""]
    if plan:
        md += ["> ⚠️ **This spec embeds real business documents** (the files in "
               f"`{plan[0]['files_dirname']}/`). Treat the whole bundle as sensitive when moving "
               "or sharing it.", ""]
    if not env_complete:
        md += ["> ⚠️ **environment-incomplete** — real work-product artifacts were not fully "
               "supplied for this workflow. Field parsing, output mapping, and file format must "
               "be verified against real files before relying on this spec.", ""]
    md += ["Paste this into Claude Code or Cursor to build an agent that performs this workflow. "
           "The attached files travel with this spec; reference them by the relative paths below.",
           ""]

    # P3 — mandatory preflight, BEFORE Intent, generated generically from the artifacts.
    md += environment.preflight_lines(plan)

    md += ["", "## 1. Intent", "", intent or "_(no intent provided)_"]
    if bundle.get("preferred_output"):
        md += ["", "**Preferred output:**", "", f"> {bundle['preferred_output']}"]
    if bundle.get("reclassified_from") == "anti_bundle" and bundle.get("original_reason_for_judgment"):
        md += ["", "**Context — originally judgment-required:** "
               f"{bundle['original_reason_for_judgment']} "
               "Treat the judgment step with a human checkpoint."]

    md += ["", "## 2. Observed examples", ""]
    if plan:
        md += ["### 2a. Real uploaded artifacts (primary — read these live; the user "
               "instruction is authoritative)", ""]
        for e in plan:
            ex = e.get("extracted") or {}
            instr = e.get("instruction") or ""
            md += [f"**{e['role']} — `{e['name']}`**"]
            md += [f"<!-- raw file: {e['rel']} -->"]
            md += [f"- **Instruction (authoritative):** {instr or '_(none provided)_'}",
                   f"- Raw file (read live): `{e['rel']}`"]
            es = e.get("extraction_status")
            if ex and es in ("ok", "partial"):
                aid = []
                if ex.get("format"):
                    aid.append(f"format `{ex['format']}`")
                if ex.get("tab_names"):
                    aid.append("sheets: " + ", ".join(str(t) for t in ex["tab_names"]))
                if ex.get("headers"):
                    aid.append("columns: " + ", ".join(str(h) for h in ex["headers"]))
                if ex.get("field_layout"):
                    aid.append("fields: " + ", ".join(f.get("label", "") for f in ex["field_layout"][:15]))
                md += [f"- _auto-read (UNVERIFIED, best-effort — not authoritative): {'; '.join(aid)}_"]
                if ex.get("text"):
                    md += ["", "> _auto-read text (unverified):_", "", "```", ex["text"][:800], "```"]
            else:
                md += [f"- _auto-read: {es or 'none'} — rely on the raw file and instruction above._"]
            md += [""]
        md += ["### 2b. Supplementary observed instances (from the event log)", ""]
    else:
        md += ["_Real captured instances, verbatim. Examples marked *illustrative* "
               "contradict the intent above and are context only — do not treat them as "
               "targets._", ""]
    if not examples:
        md += ["_(no examples selected)_"]
    for n, ex in enumerate(examples, 1):
        kind = assessments.get(ex.get("id"), "representative")
        kind = "illustrative" if str(kind).lower().startswith("illu") else "representative"
        loc = f'{ex.get("app", "")} / {ex.get("window", "")}'.strip(" /")
        md += [f"### Example {n} — `{ex.get('id')}` ({loc}, {ex.get('ts_start', '')}) — *{kind}*",
               "",
               f"> {ex.get('detail', '')}"]
        if kind == "illustrative":
            md += ["", "_Illustrative of context only — the intent overrides this; not a target._"]
        md += [""]

    md += ["## 3. Acceptance criteria",
           "",
           "_The built agent must satisfy every criterion. A wrong build should fail "
           "these loudly._", ""]
    crit = list(derived)
    crit += [f"{c}  _(live-checked against the attached file)_"
             for c in environment.instruction_criteria(plan, (env_record or {}).get("answers", []))]
    crit += [f"{c}  _(user-specified)_" for c in bundle.get("added_acceptance_criteria", [])]
    for cp in bundle.get("human_checkpoints", []):
        crit.append(_checkpoint_criterion(cp, bundle.get("steps", [])) + "  _(human checkpoint)_")
    if not env_complete:
        crit.append("Verify field parsing, output mapping, and file format against real "
                    "uploaded files — environment was incomplete at spec time.  _(environment-incomplete)_")
    if not crit:
        crit = ["_(no acceptance criteria — add some in review before building)_"]
    md += [f"- {c}" for c in crit]

    if model_out.get("notes"):
        md += ["", "## Notes", "", str(model_out["notes"])]
    return "\n".join(md) + "\n"


def assemble_manual_note(item):
    """Deterministic 'handle manually — here's why' note for an anti-bundle."""
    name = item.get("name") or item.get("id")
    md = [f"# Manual handling: {name}",
          f"<!-- {item.get('id')} | classified judgment-required — NOT built as an agent -->",
          "",
          "This workflow is **judgment-required** and is not built as an automated agent.",
          "",
          f"**Why a human is needed:** {item.get('why_judgment') or '(not specified)'}",
          "",
          f"**Recommended handling:** {item.get('recommended_handling') or '(not specified)'}",
          "",
          f"_Evidence event ids: {', '.join(item.get('evidence_event_ids', [])) or '(none)'}_",
          ""]
    return "\n".join(md)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
def _spec_filename(item_id, built):
    safe = "".join(c if c.isalnum() else "_" for c in str(item_id))
    return f"spec_{safe}.md" if built else f"spec_{safe}_manual.md"


def environment_filename(item_id):
    safe = "".join(c if c.isalnum() else "_" for c in str(item_id))
    return f"environment_{safe}.json"


def _files_dirname(item_id):
    safe = "".join(c if c.isalnum() else "_" for c in str(item_id))
    return f"spec_{safe}_files"


def _load_env(env_dir, bundle_id):
    """Load + validate a bundle's environment record from ``env_dir``, or None."""
    if not env_dir:
        return None
    p = os.path.join(env_dir, environment_filename(bundle_id))
    if not os.path.exists(p):
        return None
    try:
        return environment.validate_environment(load_json(p))
    except Exception:
        return None


def generate_specs(reviewed_path, event_log_path, model=DEFAULT_MODEL, model_fn=None,
                   env_dir=None):
    """Generate one artifact per reviewed item. Returns ``(results, report)`` where
    each result is ``{id, classification, disposition, built, filename, markdown,
    environment_status}``. ``env_dir`` (default: the reviewed file's directory) is
    where per-bundle ``environment_<id>.json`` records are read from; a missing or
    incomplete record flags the spec ``environment-incomplete`` rather than blocking.
    """
    reviewed = load_json(reviewed_path)
    event_log = load_json(event_log_path)
    report = review.ReviewReport()
    valid_ids = set(review.event_index(event_log))
    reviewed = review.validate_reviewed(reviewed, valid_ids, report)
    if env_dir is None:
        env_dir = os.path.dirname(os.path.abspath(reviewed_path))

    if model_fn is None and any(b.get("disposition") == "approve_for_build"
                                for b in reviewed["bundles"]):
        model_fn = _default_model_fn(model)

    results = []

    for b in reviewed["bundles"]:
        if b.get("disposition") != "approve_for_build":
            results.append({"id": b["id"], "classification": "bundle",
                            "disposition": b.get("disposition"), "built": False,
                            "filename": None, "markdown": None, "environment_status": None})
            continue
        env_record = _load_env(env_dir, b["id"])
        files_dir = _files_dirname(b["id"])
        plan = environment.build_export_plan(env_record, files_dir)
        examples = review.fetch_examples(event_log, b["included_example_event_ids"])
        user = build_prompt_material(b, examples, env_record)
        model_out = _parse_json_object(model_fn(SYSTEM_PROMPT, user))
        md = assemble_spec(b, examples, model_out, env_record, plan)
        results.append({"id": b["id"], "classification": "bundle",
                        "disposition": "approve_for_build", "built": True,
                        "filename": _spec_filename(b["id"], True), "markdown": md,
                        "environment_status": (env_record or {}).get("status") or "missing",
                        "files_dir": files_dir,
                        "files": [{"src": e["src"], "name": e["name"]} for e in plan]})

    for a in reviewed["anti_bundles"]:
        md = assemble_manual_note(a)
        results.append({"id": a["id"], "classification": "anti_bundle",
                        "disposition": None, "built": False,
                        "filename": _spec_filename(a["id"], False), "markdown": md,
                        "environment_status": None, "files_dir": None, "files": []})

    return results, report


def write_spec_bundle(out_dir, result):
    """Write one self-contained spec bundle into ``out_dir``: the ``spec_<id>.md``
    file plus, for build specs, a sibling ``spec_<id>_files/`` directory containing a
    byte-identical copy of every attached artifact (P1). Returns the list of
    artifacts that could not be copied (e.g. a deleted source), which the preflight
    will catch at build time; never raises on a missing source."""
    if result.get("markdown") is None:
        return []
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, result["filename"]), "w", encoding="utf-8") as f:
        f.write(result["markdown"])
    missing = []
    files = result.get("files") or []
    if files:
        fdir = os.path.join(out_dir, result["files_dir"])
        os.makedirs(fdir, exist_ok=True)
        for fe in files:
            try:
                shutil.copyfile(fe["src"], os.path.join(fdir, fe["name"]))
            except (OSError, TypeError):
                missing.append(fe["name"])
    return missing
