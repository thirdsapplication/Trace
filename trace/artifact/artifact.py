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

Model default: claude-sonnet-4-6 (single swappable default); ``model_fn`` injectable
so the engine is testable without a key/network. Spec quality matters because it is
what gets built from.
"""

import json

from . import review
from .prompt import SYSTEM_PROMPT

DEFAULT_MODEL = "claude-sonnet-4-6"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _default_model_fn(model):
    import anthropic
    client = anthropic.Anthropic()

    def call(system, user):
        resp = client.messages.create(
            model=model, max_tokens=4000, temperature=0,
            system=system, messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

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


def build_prompt_material(bundle, examples):
    """The user-turn content for one bundle (the bundle's edited intent + verbatim
    examples + criteria + checkpoints). Shared by the real call and --dry-run."""
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
    lines += ["OBSERVED EXAMPLES (verbatim — do not quote in your output):"]
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


def assemble_spec(bundle, examples, model_out):
    """Assemble the three-layer Artifact C markdown from the (verbatim) examples and
    the model's intent/criteria/assessments. The verbatim examples, user-added
    criteria, and per-checkpoint criteria are inserted deterministically."""
    name = bundle.get("name") or bundle.get("id")
    intent = (model_out.get("intent") or bundle.get("description") or "").strip()
    assessments = model_out.get("example_assessments") or {}
    derived = [str(c).strip() for c in (model_out.get("derived_acceptance_criteria") or []) if str(c).strip()]

    md = [f"# Build spec: {name}",
          f"<!-- bundle_id: {bundle.get('id')} | generated by the Trace Artifact Engine -->",
          "",
          "Paste this into Claude Code or Cursor to build an agent that performs this workflow.",
          "",
          "## 1. Intent",
          "",
          intent or "_(no intent provided)_"]
    if bundle.get("preferred_output"):
        md += ["", "**Preferred output:**", "", f"> {bundle['preferred_output']}"]
    if bundle.get("reclassified_from") == "anti_bundle" and bundle.get("original_reason_for_judgment"):
        md += ["", "**Context — originally judgment-required:** "
               f"{bundle['original_reason_for_judgment']} "
               "Treat the judgment step with a human checkpoint."]

    md += ["", "## 2. Observed examples",
           "",
           "_Real captured instances, verbatim. Examples marked *illustrative* "
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
    crit += [f"{c}  _(user-specified)_" for c in bundle.get("added_acceptance_criteria", [])]
    for cp in bundle.get("human_checkpoints", []):
        crit.append(_checkpoint_criterion(cp, bundle.get("steps", [])) + "  _(human checkpoint)_")
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


def generate_specs(reviewed_path, event_log_path, model=DEFAULT_MODEL, model_fn=None):
    """Generate one artifact per reviewed item. Returns ``(results, report)`` where
    each result is ``{id, classification, disposition, built, filename, markdown}``.
    """
    reviewed = load_json(reviewed_path)
    event_log = load_json(event_log_path)
    report = review.ReviewReport()
    valid_ids = set(review.event_index(event_log))
    reviewed = review.validate_reviewed(reviewed, valid_ids, report)

    if model_fn is None and any(b.get("disposition") == "approve_for_build"
                                for b in reviewed["bundles"]):
        model_fn = _default_model_fn(model)

    results = []

    for b in reviewed["bundles"]:
        if b.get("disposition") != "approve_for_build":
            results.append({"id": b["id"], "classification": "bundle",
                            "disposition": b.get("disposition"), "built": False,
                            "filename": None, "markdown": None})
            continue
        examples = review.fetch_examples(event_log, b["included_example_event_ids"])
        user = build_prompt_material(b, examples)
        model_out = _parse_json_object(model_fn(SYSTEM_PROMPT, user))
        md = assemble_spec(b, examples, model_out)
        results.append({"id": b["id"], "classification": "bundle",
                        "disposition": "approve_for_build", "built": True,
                        "filename": _spec_filename(b["id"], True), "markdown": md})

    for a in reviewed["anti_bundles"]:
        md = assemble_manual_note(a)
        results.append({"id": a["id"], "classification": "anti_bundle",
                        "disposition": None, "built": False,
                        "filename": _spec_filename(a["id"], False), "markdown": md})

    return results, report
