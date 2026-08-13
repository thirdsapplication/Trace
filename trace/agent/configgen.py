"""The Config Engine (Phase 1) — reviewed workflow -> agent configuration directly.

One model call per approved bundle returns STRUCTURED CONTENT (role, steps, working
rules, requirements, description), each item carrying a source tag. Deterministic
code then (1) assembles the ``system`` prompt text in a fixed section order,
(2) stamps the test and live YAML variants (every structural field is a code
constant — identical for every agent), and (3) builds the grounding record that
footnotes every instruction to its source.

This replaces the old two-step path (Artifact Engine markdown -> Compiler wrap):
no build-spec markdown is produced, and every live agent uses ONE Pipedream
connection instead of a per-app MCP table.

``model_fn(system, user) -> str`` is injectable so the whole engine runs offline
against a stub. The YAML emitter and round-trip validator are reused from the
existing ``compiler`` module; ``review`` provides the reviewed-bundles contract and
verbatim example lookup.
"""

import json
import os
import re

from . import compiler, config
from .config_prompt import SYSTEM_PROMPT
from ..artifact import review

DEFAULT_MODEL = config.DEFAULT_MODEL


# --------------------------------------------------------------------------
# Model seam + defensive JSON parse (same pattern as the other engines)
# --------------------------------------------------------------------------
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
# Inputs: reviewed bundle (+ event log + answers) -> the three engine inputs
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


# The safety-net UI posts the reserved 'safety_rules' answer. Its locked "Anything
# outbound" bullet describes RUNTIME staging (config.STAGING_DIRECTIVE, prepended to
# run triggers) and must never reach the Config Generator: the approved-execution run
# reuses the generated system prompt with an execute-for-real trigger, and a baked-in
# "must be staged" requirement makes the agent refuse. New saves no longer include the
# bullet, but answers stored by older sessions still carry it (the interview preserves
# answers across question regeneration), so it is stripped here — the one point where
# answers become prompt material.
_SAFETY_RULES_QID = "safety_rules"          # mirrors ui.app.SAFETY_RULES_ID
_RULES_HEADER = "stop and ask me when any of these is true:"
_OUTBOUND_BULLET = re.compile(r"^\s*-\s*anything outbound\b", re.IGNORECASE)


def _strip_staging_bullet(user_answers):
    """``user_answers`` minus the safety_rules outbound bullet; the whole answer is
    dropped when nothing but the header would remain."""
    out = []
    for a in user_answers:
        if isinstance(a, dict) and a.get("q") == _SAFETY_RULES_QID:
            lines = [l for l in str(a.get("answer", "")).splitlines()
                     if l.strip() and not _OUTBOUND_BULLET.match(l)]
            if not [l for l in lines if l.strip().lower() != _RULES_HEADER]:
                continue                    # only the header left: drop the answer
            a = dict(a, answer="\n".join(lines))
        out.append(a)
    return out


def build_prompt_material(workflow_description, observed_events, user_answers,
                          test_feedback=None):
    """The model's USER message: the inputs as one JSON object (also what the CLI
    ``--dry-run`` prints). ``test_feedback`` (Phase 5: dry-run corrections) is included
    only when present. Callers' inputs pass through unchanged except the safety_rules
    answer, which has its outbound-staging bullet stripped (see above)."""
    obj = {"workflow_description": workflow_description,
           "observed_events": observed_events,
           "user_answers": _strip_staging_bullet(list(user_answers or []))}
    if test_feedback:
        obj["test_feedback"] = list(test_feedback)
    return json.dumps(obj, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Deterministic assembly
# --------------------------------------------------------------------------
def _items(model_out, key):
    """The model's list under ``key`` as [{text, source}], dropping empties."""
    out = []
    for it in (model_out.get(key) or []):
        if isinstance(it, dict) and str(it.get("text", "")).strip():
            out.append({"text": str(it["text"]).strip(), "source": it.get("source")})
    return out


def _has_match_rule(working_rules):
    """True if a working rule already tells the agent to match existing examples."""
    for r in working_rules:
        low = r["text"].lower()
        if "match" in low and ("existing" in low or "example" in low):
            return True
    return False


def _has_no_dup_rule(working_rules):
    """True if a working rule already guards against duplicating prior work (Spec 7)."""
    for r in working_rules:
        low = r["text"].lower()
        if "duplicat" in low:
            return True
        if "already" in low and any(w in low for w in ("skip", "avoid", "again", "repeat")):
            return True
    return False


def _has_prompt_injection_rule(working_rules):
    """True if a working rule already guards against prompt injection (treating read
    content as untrusted data rather than instructions)."""
    for r in working_rules:
        low = r["text"].lower()
        if "injection" in low:
            return True
        if "instruction" in low and any(w in low for w in
                                        ("ignore", "untrusted", "as data", "not instruction",
                                         "do not comply", "do not follow", "don't follow")):
            return True
    return False


def final_working_rules(model_out):
    """Model working rules plus the canonical rules, each appended (with a stable key +
    discovery source) only if the model omitted it: the 'match existing examples' rule
    (P spec §5/§6.1), the anti-duplication rule (Spec 7), and the anti-prompt-injection
    rule, so every agent always carries all three."""
    rules = _items(model_out, "working_rules")
    if not _has_match_rule(rules):
        rules = rules + [{"text": config.MATCH_EXAMPLES_RULE,
                          "source": {"type": "discovery"},
                          "key": "rule_match_existing_examples"}]
    if not _has_no_dup_rule(rules):
        rules = rules + [{"text": config.NO_DUPLICATION_RULE,
                          "source": {"type": "discovery"},
                          "key": "rule_no_duplication"}]
    if not _has_prompt_injection_rule(rules):
        rules = rules + [{"text": config.PROMPT_INJECTION_RULE,
                          "source": {"type": "discovery"},
                          "key": "rule_prompt_injection"}]
    return rules


def assemble_system_text(model_out, working_rules):
    """Assemble the ``system`` prompt text in the fixed section order (§6.1)."""
    tools_and_artifacts = [str(t).strip() for t in (model_out.get("tools_and_artifacts") or [])
                           if str(t).strip()]
    steps = _items(model_out, "steps")
    requirements = _items(model_out, "requirements")

    lines = [str(model_out.get("role", "") or "").strip(), ""]
    lines.append("You work with:")
    lines += [f"- {t}" for t in tools_and_artifacts]
    lines += ["", "Steps:"]
    lines += [f"{i + 1}. {s['text']}" for i, s in enumerate(steps)]
    lines += ["", "How to do the work:"]
    lines += [f"- {r['text']}" for r in working_rules]
    lines += ["", "Requirements:"]
    lines += [f"- {q['text']}" for q in requirements]
    return "\n".join(lines)


def _envelope(bundle, model_out, system_text):
    """Stamp the live YAML envelope dict; structural fields are code constants."""
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


def build_grounding(bundle_id, steps, working_rules, requirements, unresolved=None):
    """One entry per step/working-rule/requirement, keyed by a deterministic slug,
    recording its source and whether it was directly observed (events-sourced).
    ``unresolved`` (Phase 2: interview questions the worker skipped) is recorded as a
    top-level list when non-empty — the agent falls back to reading the live
    environment for those, and the gap is auditable."""
    used, instructions = set(), []
    for prefix, items in (("step_", steps), ("rule_", working_rules), ("req_", requirements)):
        for i, item in enumerate(items):
            instructions.append(_grounding_entry(prefix, i, item, used))
    record = {"bundle_id": bundle_id, "instructions": instructions}
    if unresolved:
        record["unresolved"] = list(unresolved)
    return record


# --------------------------------------------------------------------------
# Per-bundle compile + the engine over a reviewed file
# --------------------------------------------------------------------------
def compile_config(bundle, observed_events, user_answers, model_fn, model=DEFAULT_MODEL,
                   unresolved=None, test_feedback=None):
    """Run the model once and assemble the live config + grounding for one bundle.
    Returns ``{live, grounding, content}``. ``model_fn`` is required (callers
    that want the default should construct it; tests inject a stub). ``unresolved``
    (skipped interview questions) is recorded in the grounding record.
    ``test_feedback`` (worker corrections) is AUTHORITATIVE and folded in."""
    workflow_description = _workflow_description(bundle)
    user_msg = build_prompt_material(workflow_description, observed_events, user_answers,
                                     test_feedback)
    model_out = _parse_json_object(model_fn(SYSTEM_PROMPT, user_msg))

    working_rules = final_working_rules(model_out)
    steps = _items(model_out, "steps")
    requirements = _items(model_out, "requirements")
    system_text = assemble_system_text(model_out, working_rules)
    envelope = build_envelope(bundle, model_out, system_text)
    grounding = build_grounding(bundle.get("id"), steps, working_rules, requirements,
                                unresolved=unresolved)
    return {"live": envelope, "grounding": grounding, "content": model_out}


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
