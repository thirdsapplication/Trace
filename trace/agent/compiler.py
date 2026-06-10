"""Component A — the Agent Compiler (pure transformation, no network).

Wraps an existing ``spec_<id>.md`` into a Managed Agents agent definition
(``agent_<id>.{test,live}.yaml``). The spec's substantive content travels into the
``system`` field VERBATIM; the Compiler performs only mechanical adaptations
(framing prepend, artifact-path rewrite to the mount, dropping coding-agent wrapper
lines, rewording the preflight trigger). ``spec_<id>.md`` is never modified.

No fixture/sender/tab/SKU/bundle-id appears here; connectors derive from observed
application identity via the editable table in ``config``.
"""

import io
import re

import yaml

from . import config


# --- YAML emission: literal block scalar for the system prompt -------------
class _Literal(str):
    """Marker so PyYAML emits this string as a ``|`` literal block scalar."""


def _repr_literal(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(_Literal, _repr_literal, Dumper=yaml.SafeDumper)


def emit_yaml(envelope, unmapped_apps=None):
    """Serialize an envelope dict to YAML: 2-space indent, spaces only (never tabs),
    the system prompt as a literal block scalar. Unmapped observed apps are appended
    as a YAML comment (P4)."""
    e = dict(envelope)
    if "system" in e:
        e["system"] = _Literal(e["system"].rstrip("\n"))
    buf = io.StringIO()
    yaml.dump(e, buf, Dumper=yaml.SafeDumper, sort_keys=False,
              default_flow_style=False, width=10 ** 9, allow_unicode=True)
    text = buf.getvalue()
    if "\t" in text:                       # guard the failure mode from manual testing
        text = text.replace("\t", "  ")
    if unmapped_apps:
        text += f"\n# unmapped observed apps (no MCP connector): {', '.join(sorted(set(unmapped_apps)))}\n"
    return text


def validate_yaml(text):
    """Parse the emitted YAML back to confirm it is well-formed and tab-free."""
    if "\t" in text:
        raise ValueError("emitted YAML contains a tab character")
    return yaml.safe_load(text)


# --- mechanical spec adaptation (P1) ---------------------------------------
_WRAPPER_PATTERNS = [
    re.compile(r"(?im)^.*paste this into (claude code|cursor).*$"),
    re.compile(r"(?im)^.*build an agent that performs this workflow.*$"),
]


def adapt_spec(spec_md, mount_path):
    """Apply only the mechanical adaptations of P1 to the spec text:
    - rewrite ``./spec_<id>_files/<f>`` (and the bare form) to ``<mount>/<f>``,
    - drop wrapper lines that address a coding agent,
    - reword the preflight trigger from "before writing any code" to
      "before processing any task".
    Returns the adapted text; the on-disk spec is untouched."""
    text = spec_md
    # path rewrite: ./spec_<id>_files/<file> or spec_<id>_files/<file> -> <mount>/<file>
    text = re.sub(r"\.?/?spec_[A-Za-z0-9_]+_files/", mount_path.rstrip("/") + "/", text)
    # drop coding-agent wrapper lines
    for pat in _WRAPPER_PATTERNS:
        text = pat.sub("", text)
    # reword preflight trigger (coding-agent phrasing -> running-agent phrasing)
    text = text.replace("before writing any code", "before processing any task")
    text = text.replace("before you write code", "before processing any task")
    # collapse the blank lines left by dropped wrapper lines
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


# --- connector derivation from observed apps (P4) --------------------------
def observed_apps(bundle, event_log):
    """The distinct application identities seen in the bundle's evidence events,
    in first-seen order."""
    idx = {e.get("id"): e for e in event_log if isinstance(e, dict)}
    seen, out = set(), []
    for eid in bundle.get("evidence_event_ids", []):
        app = (idx.get(eid, {}).get("app") or "").strip()
        if app and app.lower() not in seen:
            seen.add(app.lower())
            out.append(app)
    return out


def derive_connectors(apps, table=None):
    """Map observed apps to MCP server entries via the table. Returns
    ``(connectors, unmapped)`` where connectors is a de-duplicated list of
    ``{name, url}`` and unmapped is the apps with no table row."""
    table = config.APP_MCP_TABLE if table is None else table
    connectors, seen_names, unmapped = [], set(), []
    for app in apps:
        low = app.lower()
        entry = next((v for k, v in table.items() if k in low), None)
        if entry is None:
            unmapped.append(app)
        elif entry["name"] not in seen_names:
            seen_names.add(entry["name"])
            connectors.append(dict(entry))
    return connectors, unmapped


# --- envelope construction (P1–P5) -----------------------------------------
def _description(bundle, mode):
    intent = (bundle.get("description") or bundle.get("name") or "").strip()
    one_line = intent.split("\n")[0][:160]
    if mode == "test":
        return f"{one_line} Sandbox test build — no external connections; outbound actions are emitted as text."
    return f"{one_line} Live build — operates on connected services."


def build_envelope(bundle, spec_md, event_log, mode, model=None,
                   mount_path=None, table=None):
    """Build the agent-envelope dict for ``mode`` ('test' | 'live'). Returns
    ``(envelope_dict, unmapped_apps)``."""
    if mode not in ("test", "live"):
        raise ValueError("mode must be 'test' or 'live'")
    model = model or config.DEFAULT_MODEL
    mount = mount_path or config.MOUNT_PATH
    workflow = bundle.get("name") or bundle.get("id") or "this workflow"

    framing = (config.FRAMING_TEST if mode == "test" else config.FRAMING_LIVE).format(
        workflow=workflow, mount=mount, outputs=config.OUTPUTS_PATH)
    system = framing + "\n\n" + adapt_spec(spec_md, mount)

    name = f"{workflow} (test)" if mode == "test" else workflow
    env = {
        "name": name,
        "description": _description(bundle, mode),
        "model": model,
        "system": system,
        "tools": [{"type": config.AGENT_TOOLSET_TYPE}],
    }

    unmapped = []
    if mode == "live":
        connectors, unmapped = derive_connectors(observed_apps(bundle, event_log), table)
        for c in connectors:
            env["tools"].append({
                "type": "mcp_toolset",
                "mcp_server_name": c["name"],
                "default_config": {
                    "permission_policy": {"type": config.APPROVAL_PERMISSION_POLICY},  # P5
                },
            })
        if connectors:
            env["mcp_servers"] = [{"name": c["name"], "url": c["url"], "type": "url"}
                                  for c in connectors]
    return env, unmapped


def compile_agent(bundle, spec_md, event_log, model=None, mount_path=None, table=None):
    """Compile both envelopes for a bundle. Returns a dict with, per mode, the
    validated envelope dict and its serialized, schema-clean YAML text."""
    out = {}
    for mode in ("test", "live"):
        env, unmapped = build_envelope(bundle, spec_md, event_log, mode, model, mount_path, table)
        validate_envelope(env, mode)
        text = emit_yaml(env, unmapped if mode == "live" else None)
        validate_yaml(text)                 # round-trip: well-formed + tab-free
        out[mode] = {"dict": env, "yaml": text, "unmapped_apps": unmapped}
    return out


def validate_envelope(env, mode):
    """Structural validation against the frozen agent-envelope schema (required
    fields, tool shapes, test=no MCP, live write policies require approval). Raises
    ValueError on a violation."""
    for k in ("name", "description", "model", "system", "tools"):
        if not env.get(k):
            raise ValueError(f"envelope missing required field {k!r}")
    if not (isinstance(env["tools"], list) and env["tools"]):
        raise ValueError("envelope tools must be a non-empty list")
    if env["tools"][0].get("type") != config.AGENT_TOOLSET_TYPE:
        raise ValueError("first tool must be the agent toolset")
    mcp_tools = [t for t in env["tools"] if t.get("type") == "mcp_toolset"]
    if mode == "test":
        if mcp_tools or env.get("mcp_servers"):
            raise ValueError("test envelope must contain no MCP servers")
    else:  # live: every MCP write capability must require approval (P5)
        for t in mcp_tools:
            pol = (t.get("default_config", {}).get("permission_policy", {}) or {}).get("type")
            if pol != config.APPROVAL_PERMISSION_POLICY:
                raise ValueError("live MCP tools must default to an approval-required policy")
    return True
