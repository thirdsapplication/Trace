"""The Agent Author engine (Prompt Library v2) — reviewed workflow -> agent, in one call.

One model call per approved bundle writes the deployed agent's system prompt as
STRUCTURED CONTENT — role, apps, preflight checks, steps, working rules, acceptance
criteria, stop conditions — plus the unknowns the interview needs, every item carrying
one source tag. Deterministic code then (1) assembles the ``system`` prompt text in a
fixed section order, (2) stamps the live YAML envelope (every structural field is a
code constant — identical for every agent), and (3) builds the grounding record that
footnotes every instruction to its source.

This is the merge of two earlier engines: the Artifact engine (a markdown build spec
with acceptance criteria) and the Config engine (a content-only config call). One
call now writes both what the agent does and what done looks like.

The same engine runs twice per workflow: once before the interview, with no answers,
to draft the agent and surface its unknowns (the Gap Detector turns those into
questions); and once at build, with the worker's answers folded in.

``model_fn(system, user) -> str`` is injectable so the whole engine runs offline
against a stub. The YAML emitter and round-trip validator are reused from the
existing ``compiler`` module; ``review`` provides the reviewed-bundles contract and
verbatim example lookup.
"""

import json
import os
import re

from . import compiler, config, model_call
from .config_prompt import SYSTEM_PROMPT
from ..artifact import review

DEFAULT_MODEL = config.DEFAULT_MODEL

SOURCE_TYPES = ("events", "discovery", "user_description", "user_answer", "user_test_feedback")
LENSES = ("capture_blind_spot", "undetermined_decision")

# The content sections, in the order they are rendered into the system prompt and
# footnoted in the grounding record. (prefix, key, heading) — the heading is None for
# a section that is grounded but not rendered.
SECTIONS = (
    ("preflight_", "preflight", "Before every run:"),
    ("step_", "steps", "Steps:"),
    ("rule_", "working_rules", "Working rules:"),
    ("criterion_", "acceptance_criteria", "Done looks like:"),
    ("stop_", "stop_conditions", "Stop and ask when:"),
)


# --------------------------------------------------------------------------
# Model seam + defensive JSON parse (same pattern as the other engines)
# --------------------------------------------------------------------------
def _default_model_fn(model, timeout=None, max_retries=None):
    """A caller for ``model``. ``timeout``/``max_retries`` bound the client: a bare
    anthropic.Anthropic() defaults to a 10-minute-per-attempt timeout with automatic
    retries, which is fine for a background build but not for the activity view's
    summarizer, which runs inside a user-facing GET (see AppState._summarize_model_fn)."""
    import anthropic
    kw = {}
    if timeout is not None:
        kw["timeout"] = timeout
    if max_retries is not None:
        kw["max_retries"] = max_retries
    client = anthropic.Anthropic(**kw)

    def call(system, user):
        # 16000, not the old 4000: Fable thinks before it answers and that reasoning
        # is billed against max_tokens, so a small ceiling can be spent before the
        # JSON starts. 16000 is also the most a non-streaming request may ask for.
        return model_call.create_text(client, model, system, user, max_tokens=16000)

    return call


def _parse_json_object(text):
    if not text:
        return {}
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else {}
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
# Inputs: reviewed bundle (+ event log + answers) -> the engine inputs
# --------------------------------------------------------------------------
def _workflow_description(bundle):
    """The ``workflow_description`` engine input for one reviewed bundle. Carries the
    worker's review-step correction (``user_description``) ONLY when present, so the
    prompt material is unchanged for uncorrected bundles."""
    wd = {
        "name": bundle.get("name", ""),
        "description": bundle.get("description", ""),
        "trigger": bundle.get("trigger", ""),
        "steps": list(bundle.get("steps", []) or []),
        "evidence_event_ids": list(bundle.get("evidence_event_ids", []) or []),
    }
    ud = str(bundle.get("user_description", "") or "").strip()
    if ud:
        wd["user_description"] = ud
    return wd


def make_engine_inputs(bundle, event_log, user_answers=None):
    """Build ``(workflow_description, observed_events, user_answers)`` for one bundle.
    ``observed_events`` carries each evidence event's id + plain ``detail`` text."""
    workflow_description = _workflow_description(bundle)
    observed_events = [{"id": ex["id"], "description": ex.get("detail", "")}
                       for ex in review.fetch_examples(event_log, workflow_description["evidence_event_ids"])]
    return workflow_description, observed_events, list(user_answers or [])


def build_prompt_material(workflow_description, observed_events, user_answers,
                          test_feedback=None):
    """The model's USER message: the inputs as one JSON object (also what the CLI
    ``--dry-run`` prints). ``test_feedback`` (Phase 5: dry-run corrections) is included
    only when present, so existing callers/output are unchanged."""
    obj = {"workflow_description": workflow_description,
           "observed_events": observed_events,
           "user_answers": list(user_answers or [])}
    if test_feedback:
        obj["test_feedback"] = list(test_feedback)
    return json.dumps(obj, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Normalising the model's content
# --------------------------------------------------------------------------
def _ids(v):
    return [str(x).strip() for x in (v if isinstance(v, list) else [v] if v else [])
            if str(x).strip()]


def _normalize_source(item):
    """One item's source tag as the ``{type, ...}`` dict the grounding record has always
    carried. The v2 prompt writes the flat form — ``"source": "events"`` with a sibling
    ``evidence`` list, ``"source": "user_answer"`` with a sibling ``q`` — and the older
    object form ``{"type": "events", "ids": [...]}`` is accepted unchanged. An
    unrecognised tag becomes ``None`` rather than a guess."""
    src = item.get("source")
    if isinstance(src, dict):
        stype = src.get("type")
        if stype not in SOURCE_TYPES:
            return None
        out = {"type": stype}
        if stype == "events":
            out["ids"] = _ids(src.get("ids")) or _ids(item.get("evidence"))
        elif stype == "user_answer":
            out["q"] = str(src.get("q") or item.get("q") or "").strip()
        elif stype == "user_test_feedback":
            note = str(src.get("note") or item.get("note") or "").strip()
            if note:
                out["note"] = note
        return out
    stype = str(src or "").strip()
    if stype not in SOURCE_TYPES:
        return None
    out = {"type": stype}
    if stype == "events":
        out["ids"] = _ids(item.get("evidence"))
    elif stype == "user_answer":
        out["q"] = str(item.get("q") or "").strip()
    elif stype == "user_test_feedback":
        note = str(item.get("note") or "").strip()
        if note:
            out["note"] = note
    return out


def _items(model_out, key):
    """The model's list under ``key`` as [{text, source}], dropping empties. A step's
    ``branch_if`` is folded into its text so the condition is never lost."""
    out = []
    for it in (model_out.get(key) or []):
        if not isinstance(it, dict):
            continue
        text = str(it.get("text", "") or "").strip()
        if not text:
            continue
        cond = str(it.get("branch_if") or "").strip()
        if cond and not text.lower().startswith("if "):
            text = f"If {cond}: {text}"
        out.append({"text": text, "source": _normalize_source(it)})
    return out


def _unknowns(model_out):
    """The model's unknowns as ``[{what, lens, why_it_matters, evidence}]``."""
    out = []
    for u in (model_out.get("unknowns") or []):
        if isinstance(u, dict):
            what = str(u.get("what") or "").strip()
            if not what:
                continue
            lens = u.get("lens") if u.get("lens") in LENSES else "undetermined_decision"
            out.append({"what": what, "lens": lens,
                        "why_it_matters": str(u.get("why_it_matters") or "").strip(),
                        "evidence": _ids(u.get("evidence"))})
        elif str(u or "").strip():
            out.append({"what": str(u).strip(), "lens": "undetermined_decision",
                        "why_it_matters": "", "evidence": []})
    return out


def normalize_content(model_out):
    """The canonical content record: every section as [{text, source}], the apps as
    strings, the unknowns normalised. This is what the build writes out and what the
    Gap Detector reads as the draft agent."""
    content = {
        "name": str(model_out.get("name", "") or "").strip(),
        "description": str(model_out.get("description", "") or "").strip(),
        "role": str(model_out.get("role", "") or "").strip(),
        "apps": _ids(model_out.get("apps") or model_out.get("tools_and_artifacts")),
    }
    for _, key, _ in SECTIONS:
        content[key] = _items(model_out, key)
    content["unknowns"] = _unknowns(model_out)
    return content


# --------------------------------------------------------------------------
# Deterministic assembly
# --------------------------------------------------------------------------
def assemble_system_text(model_out):
    """Assemble the ``system`` prompt text in a fixed order: role; the apps it works
    in; what it checks before every run (preflight); the numbered steps; its working
    rules; what done looks like (acceptance criteria); when it stops and asks; then the
    two standing rules.

    Empty sections are omitted, heading and all, so a workflow with no stop
    conditions does not carry an empty "Stop and ask when:" — a heading with nothing
    under it reads as an instruction with no object. The standing rules are fixed
    framing appended by code, never generated content.
    """
    content = normalize_content(model_out)
    lines = [content["role"], ""]
    lines.append("You work with:")
    lines += [f"- {t}" for t in content["apps"]]
    for _, key, heading in SECTIONS:
        items = content[key]
        if not items:
            continue
        lines += ["", heading]
        if key == "steps":
            lines += [f"{i + 1}. {s['text']}" for i, s in enumerate(items)]
        else:
            lines += [f"- {s['text']}" for s in items]
    lines += ["", "Always:"]
    lines += [f"- {r}" for r in config.STANDING_RULES]
    return "\n".join(lines)


def _envelope(bundle, model_out, system_text):
    """Stamp the live YAML envelope dict; structural fields are code constants. The
    agent's platform NAME is the bundle's, not the model's: the memory store, the
    deployment names and the cleanup sweep all key on it, so it must not change
    between the draft run and the build run."""
    name = str(bundle.get("name") or bundle.get("id") or "Agent").strip()
    description = str(model_out.get("description", "") or bundle.get("description", "")
                      or name).strip()
    env = {
        "name": name,
        "description": description,
        "model": config.DEFAULT_MODEL,
        "system": system_text,
        # Template only: a single nominal Pipedream server with a base URL (no
        # externalUserId yet — unknown until deploy). The Deployer rewrites mcp_servers
        # + mcp_toolset to the customer's per-app URLs at create time
        # (config.pipedream_mcp_servers); the default permission policy is fixed here.
        "mcp_servers": [{"name": config.PIPEDREAM_MCP_NAME, "type": "url",
                         "url": config.PIPEDREAM_MCP_BASE}],
        "tools": [
            {"type": config.AGENT_TOOLSET_TYPE},
            {"type": config.MCP_TOOLSET_TYPE,
             "mcp_server_name": config.PIPEDREAM_MCP_NAME,
             "default_config": {"permission_policy": {"type": config.LIVE_PERMISSION_POLICY}}},
        ],
        "skills": [],
        "metadata": {"trace_bundle_id": bundle.get("id"), "variant": "live"},
    }
    return env


def validate_config_envelope(env):
    """Structural validation: required fields, the fixed toolset, and the live MCP rules
    (>=1 Pipedream server, every mcp_toolset on the configured permission policy). Raises
    on a violation."""
    for k in ("name", "description", "model", "system", "tools"):
        if not env.get(k):
            raise ValueError(f"config envelope missing required field {k!r}")
    if not (isinstance(env["tools"], list) and env["tools"]):
        raise ValueError("config envelope tools must be a non-empty list")
    if env["tools"][0].get("type") != config.AGENT_TOOLSET_TYPE:
        raise ValueError("first tool must be the agent toolset")
    mcp_tools = [t for t in env["tools"] if t.get("type") == config.MCP_TOOLSET_TYPE]
    # The config-gen template carries one nominal server; the Deployer expands it to one
    # server per app (config.pipedream_mcp_servers) once the customer is known.
    servers = env.get("mcp_servers") or []
    if not servers:
        raise ValueError("live config must declare at least one Pipedream MCP server")
    if not all((s.get("name") or "").startswith(config.PIPEDREAM_MCP_NAME) for s in servers):
        raise ValueError("live config MCP servers must be Pipedream servers")
    if not mcp_tools:
        raise ValueError("live config must include the Pipedream mcp_toolset")
    for t in mcp_tools:
        pol = (t.get("default_config", {}).get("permission_policy", {}) or {}).get("type")
        if pol != config.LIVE_PERMISSION_POLICY:
            raise ValueError("live mcp_toolset must use the configured live permission policy")
    return True


def build_envelope(bundle, model_out, system_text):
    """The live envelope as ``{dict, yaml}`` (YAML via the reused emitter)."""
    env = _envelope(bundle, model_out, system_text)
    validate_config_envelope(env)
    text = compiler.emit_yaml(env)
    compiler.validate_yaml(text)                # round-trip: well-formed + tab-free
    return {"dict": env, "yaml": text}


# --------------------------------------------------------------------------
# Grounding record
# --------------------------------------------------------------------------
def _slug(text, n_words=4, maxlen=40):
    words = re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split()
    s = "_".join(words[:n_words])[:maxlen].strip("_")
    return s or "item"


def _grounding_entry(prefix, i, item, used):
    key = item.get("key") or f"{prefix}{i + 1}_{_slug(item['text'])}"
    base, k = key, 2
    while key in used:
        key = f"{base}_{k}"
        k += 1
    used.add(key)
    source = item.get("source")
    observed = bool(isinstance(source, dict) and source.get("type") == "events")
    return {"key": key, "text": item["text"], "source": source, "observed": observed}


def build_grounding(bundle_id, content, unresolved=None):
    """One entry per preflight check, step, working rule, acceptance criterion and stop
    condition, keyed by a deterministic prefixed slug, recording its source and whether
    it was directly observed (events-sourced). ``content`` is a ``normalize_content``
    record. ``unresolved`` (interview questions the worker skipped) is recorded as a
    top-level list when non-empty — the agent falls back to reading the live
    environment for those, and the gap is auditable. The unknowns the author left
    open are recorded too, so an audit can see what the interview was asked to close.
    """
    used, instructions = set(), []
    for prefix, key, _ in SECTIONS:
        for i, item in enumerate(content.get(key) or []):
            instructions.append(_grounding_entry(prefix, i, item, used))
    record = {"bundle_id": bundle_id, "instructions": instructions}
    if content.get("unknowns"):
        record["unknowns"] = list(content["unknowns"])
    if unresolved:
        record["unresolved"] = list(unresolved)
    return record


# --------------------------------------------------------------------------
# Per-bundle compile + the engine over a reviewed file
# --------------------------------------------------------------------------
def compile_config(bundle, observed_events, user_answers, model_fn, model=DEFAULT_MODEL,
                   unresolved=None, test_feedback=None):
    """Run the Agent Author once and assemble the live config + grounding for one
    bundle. Returns ``{live, grounding, content}``, where ``content`` is the normalised
    content record (the draft agent the Gap Detector reads, unknowns included).
    ``model_fn`` is required (callers that want the default should construct it; tests
    inject a stub). ``unresolved`` (skipped interview questions) is recorded in the
    grounding record. ``user_answers`` and ``test_feedback`` are AUTHORITATIVE and
    folded in by the model."""
    workflow_description = _workflow_description(bundle)
    user_msg = build_prompt_material(workflow_description, observed_events, user_answers,
                                     test_feedback)
    model_out = _parse_json_object(model_fn(SYSTEM_PROMPT, user_msg))

    content = normalize_content(model_out)
    system_text = assemble_system_text(model_out)
    envelope = build_envelope(bundle, model_out, system_text)
    grounding = build_grounding(bundle.get("id"), content, unresolved=unresolved)
    return {"live": envelope, "grounding": grounding, "content": content}


def draft_agent(bundle, event_log, model_fn):
    """The Agent Author's first pass for one bundle, with no answers yet: the content
    record whose ``unknowns`` the Gap Detector turns into questions."""
    _, observed_events, _ = make_engine_inputs(bundle, event_log)
    return compile_config(bundle, observed_events, [], model_fn)["content"]


def _interview_path(answers_dir, bundle_id):
    if not answers_dir:
        return None
    safe = "".join(c if c.isalnum() else "_" for c in str(bundle_id))
    p = os.path.join(answers_dir, f"interview_{safe}.json")
    return p if os.path.exists(p) else None


def _read_interview(answers_dir, bundle_id):
    p = _interview_path(answers_dir, bundle_id)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_answers(answers_dir, bundle_id):
    """Load the worker's interview answers (Phase 2 writes ``interview_<id>.json``) as
    a list, or ``[]``. Defensive: a missing or malformed file yields ``[]``."""
    data = _read_interview(answers_dir, bundle_id)
    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get("answers", [])
    return data if isinstance(data, list) else []


def _load_unresolved(answers_dir, bundle_id):
    """The ids of interview questions the worker skipped, or ``[]``."""
    data = _read_interview(answers_dir, bundle_id)
    if isinstance(data, dict):
        u = data.get("unresolved", [])
        return u if isinstance(u, list) else []
    return []


def _load_test_feedback(answers_dir, bundle_id):
    """Load the worker's dry-run corrections (Phase 5 writes ``feedback_<id>.json``) as
    a list, or ``[]``. Defensive: a missing/malformed file yields ``[]``."""
    if not answers_dir:
        return []
    safe = "".join(c if c.isalnum() else "_" for c in str(bundle_id))
    p = os.path.join(answers_dir, f"feedback_{safe}.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("corrections", [])
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def generate_configs(reviewed_path, event_log_path, model=DEFAULT_MODEL, model_fn=None,
                     answers_dir=None):
    """Generate configs for every approved bundle in ``reviewed_path``. Returns
    ``(results, report)`` where each result is ``{id, built, live_yaml, grounding}``.
    Non-approved bundles and anti-bundles produce ``built=False`` with ``None``
    payloads — NO markdown."""
    with open(reviewed_path, encoding="utf-8") as f:
        reviewed = json.load(f)
    with open(event_log_path, encoding="utf-8") as f:
        event_log = json.load(f)

    report = review.ReviewReport()
    valid_ids = set(review.event_index(event_log))
    reviewed = review.validate_reviewed(reviewed, valid_ids, report)
    if answers_dir is None:
        answers_dir = os.path.dirname(os.path.abspath(reviewed_path))

    approved = [b for b in reviewed["bundles"] if b.get("disposition") == "approve_for_build"]
    if model_fn is None and approved:
        model_fn = _default_model_fn(model)

    results = []
    for b in reviewed["bundles"]:
        if b.get("disposition") != "approve_for_build":
            results.append({"id": b["id"], "built": False,
                            "live_yaml": None, "grounding": None})
            continue
        _, observed_events, _ = make_engine_inputs(b, event_log)
        user_answers = _load_answers(answers_dir, b["id"])
        unresolved = _load_unresolved(answers_dir, b["id"])
        test_feedback = _load_test_feedback(answers_dir, b["id"])
        compiled = compile_config(b, observed_events, user_answers, model_fn, model,
                                  unresolved=unresolved, test_feedback=test_feedback)
        results.append({"id": b["id"], "built": True,
                        "live_yaml": compiled["live"]["yaml"],
                        "grounding": compiled["grounding"]})
    for a in reviewed["anti_bundles"]:
        results.append({"id": a["id"], "built": False,
                        "live_yaml": None, "grounding": None})
    return results, report
