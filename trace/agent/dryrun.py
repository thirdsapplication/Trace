"""Phase 5 — the real-data dry run + plain-language replay.

Trace's BACKEND reads a READ-ONLY snapshot of the worker's real data through the
Phase-4 connection, saves the destination as a COPY, and mounts that copy into a
sandbox session running the TEST-variant agent (no connection — so it physically
cannot touch the real source). The transcript is rendered into a short plain replay
the worker can judge; a thumbs-down correction feeds back into Phase-1 generation.

Offline: ``reader`` (a ConnectClient), ``client`` (a ManagedAgentsClient), and the
replay ``model_fn`` are all injected, so the whole flow runs against stubs. The
Pipedream READ calls and the Files-API mount are external BETA — confirm against docs.
"""

import json
import os
import re

from . import config, deployer, gate
from .replay_prompt import SYSTEM_PROMPT

_DEST_HINTS = ("sheet", "drive", "doc", "calendar")     # destination-ish app slugs


def _parse_json_array(text):
    """Defensive parse of the replay model's JSON array of strings (mirrors
    configgen._parse_json_object but returns a list)."""
    if not text:
        return []
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        d = json.loads(t)
    except json.JSONDecodeError:
        s, e = t.find("["), t.rfind("]")
        if s == -1 or e <= s:
            return []
        try:
            d = json.loads(t[s:e + 1])
        except json.JSONDecodeError:
            return []
    return [str(x).strip() for x in d if str(x).strip()] if isinstance(d, list) else []


def render_replay(transcript, model_fn):
    """One model call: the technical transcript -> a list of short plain sentences.
    The raw transcript is never surfaced to the worker — only this list is."""
    return _parse_json_array(model_fn(SYSTEM_PROMPT, transcript or ""))


def _usable_apps(connection_record):
    return [(slug, a) for slug, a in (connection_record or {}).get("apps", {}).items()
            if a.get("usable")]


def _destination_text(dest):
    """Render a read destination snapshot ({columns, rows, raw}) as the worker's COPY
    file text (a small CSV-like view of the real columns + existing rows)."""
    if not isinstance(dest, dict):
        return str(dest or "")
    cols = dest.get("columns") or []
    rows = dest.get("rows") or []
    if cols or rows:
        lines = [",".join(str(c) for c in cols)] if cols else []
        for r in rows:
            lines.append(",".join(str(c) for c in (r if isinstance(r, (list, tuple)) else [r])))
        return "\n".join(lines) + "\n"
    return str(dest.get("raw", "") or "")


def read_snapshot(reader, connection_record, bundle, event_log, save_dir):
    """READ-ONLY: pull the destination artifact + one recent (held-out) trigger through
    the connection, save the destination as a COPY file. Returns
    ``{destination_path, trigger_text, trigger_source}``. Falls back to a trigger
    reconstructed from the capture when no recent real instance can be pulled."""
    uid = (connection_record or {}).get("external_user_id")
    usable = _usable_apps(connection_record)
    slugs = [s for s, _ in usable]
    trigger_slug = slugs[0] if slugs else None
    dest_slug = next((s for s in slugs if any(h in s for h in _DEST_HINTS)),
                     slugs[-1] if slugs else None)
    binding = lambda s: (dict(usable).get(s) or {}).get("binding_ref")

    # held-out trigger (a recent real instance the agent was almost certainly NOT built from)
    base = (bundle.get("trigger") or "").strip()
    trigger_text, trigger_source = "", "reconstructed"
    if reader is not None and trigger_slug:
        try:
            recent = reader.read_recent_trigger(uid, trigger_slug, binding(trigger_slug), limit=1)
        except Exception:
            recent = []
        if recent:
            detail = str(recent[0].get("detail", "") or "").strip()
            trigger_text = f"{base}\n\n{detail}".strip() if base else detail
            trigger_source = "connection"
    if not trigger_text:
        trigger_text = gate.generate_test_trigger(bundle, event_log)   # fallback from capture

    # destination artifact -> COPY file
    destination_path = None
    if reader is not None and dest_slug:
        try:
            dest = reader.read_destination(uid, dest_slug, binding(dest_slug))
        except Exception:
            dest = None
        if dest:
            os.makedirs(save_dir, exist_ok=True)
            destination_path = os.path.join(save_dir, f"destination_{dest_slug}.csv")
            with open(destination_path, "w", encoding="utf-8") as f:
                f.write(_destination_text(dest))
    return {"destination_path": destination_path, "trigger_text": trigger_text,
            "trigger_source": trigger_source}


def run_dry_run(bundle, test_envelope_dict, snapshot, client, replay_fn, events_path=None,
                round=0):
    """Mount the real-data COPY into a sandbox, run the TEST agent on it, DOWNLOAD what
    it wrote to the copy, and render a plain replay. The agent has no connection, so the
    real source is structurally untouched. Returns the dry-run record."""
    mount = config.MOUNT_PATH.rstrip("/")
    uploaded_ids, resources = [], []
    if snapshot.get("destination_path"):
        fid = client.upload_file(snapshot["destination_path"])
        uploaded_ids.append(fid)
        resources.append({"file_id": fid,
                          "mount_path": f"{mount}/{os.path.basename(snapshot['destination_path'])}"})

    agent = client.create_or_reuse_agent(test_envelope_dict)
    agent_id = agent.get("id") if isinstance(agent, dict) else agent
    environment_id = client.create_environment(config.ENVIRONMENT_DEFAULTS)
    sid = client.create_session(agent_id, environment_id, resources,
                                title=f"Trace dry run — {bundle.get('id')}")

    try:
        events = client.send_trigger_and_collect(sid, snapshot["trigger_text"])
        status, error = deployer._run_status(events)
    except Exception as e:
        events = [{"type": "client.error", "text": str(e), "name": None}]
        status, error = "errored", str(e)
    try:
        outputs, _warnings = client.download_outputs(sid, exclude_ids=uploaded_ids)
    except Exception:
        outputs = {}

    deployer._persist_events(events_path, bundle,
                             [{"trigger": snapshot["trigger_text"], "session_id": sid,
                               "status": status, "error": error, "events": events,
                               "output_files": list(outputs.keys())}])

    transcript = gate.render_transcript(events, outputs)
    replay = render_replay(transcript, replay_fn)
    return {"bundle_id": bundle.get("id"), "replay": replay, "outputs": outputs,
            "trigger_source": snapshot.get("trigger_source"), "session_id": sid,
            "status": status, "error": error, "round": round,
            "real_source_untouched": True, "transcript": transcript}


def dry_run(bundle, test_envelope_dict, connection_record, event_log, save_dir,
            reader, client, replay_fn, events_path=None, round=0):
    """Full Phase-5 dry run for one bundle: read the snapshot (read-only), run the TEST
    agent on the mounted COPY, and return the record with its plain replay."""
    snapshot = read_snapshot(reader, connection_record, bundle, event_log, save_dir)
    return run_dry_run(bundle, test_envelope_dict, snapshot, client, replay_fn,
                       events_path=events_path, round=round)
