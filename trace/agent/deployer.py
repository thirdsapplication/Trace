"""Component B — the Test Deployer (manual-evaluation mode).

The single question this flow answers right now is: *can Trace take a compiled
agent envelope, create (or reuse) that agent on Claude Managed Agents, run a
session, and have the agent actually execute the workflow?* Evaluation of the run
is done by the **user**, in the Console (and/or by pasting the transcript into a
chat model). So this deployer:

  - uploads the Review 2 artifact files and mounts them (read-only),
  - creates OR REUSES the agent (same name -> reuse, never accumulate),
  - runs ONE session per observed instance (default capped at 1) sending ONLY the
    trigger (zero coaching, P9),
  - returns a POINTER record — agent id/name, session id + Console link, status,
    and the NAMES of files the agent wrote under the outputs path (listed, not
    downloaded),
  - persists the full event stream locally as a backup.

It does NOT grade (the grader stays in the tree, disconnected) and it does NOT
delete anything: the session must remain visible in the Console for manual review.
Cleanup is a separate, explicit, human-initiated action (``client.cleanup``).

The ``client`` is injected so the whole flow runs offline against a stub.
"""

import json
import os

from . import config
from .trigger import generate_triggers


def _persist_events(events_path, bundle, records):
    """Write the full, untruncated captured stream to disk the instant it is
    collected — a local backup if the Console session is ever purged. Best-effort:
    a write error must never crash the run."""
    if not events_path:
        return
    try:
        with open(events_path, "w", encoding="utf-8") as f:
            json.dump({"bundle_id": bundle.get("id"), "instances": records},
                      f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _run_status(events):
    """``(status, error)`` from the captured stream: 'errored' if it reported a
    failure or never reached a terminal event, else 'completed'."""
    for e in events or []:
        if not isinstance(e, dict):
            continue
        et = e.get("type") or ""
        if "failed" in et or et in ("client.error", "client.timeout", "client.download_error"):
            return "errored", (e.get("text") or et)
    return "completed", None


def run_test(bundle, spec_md, env_record, event_log, test_envelope_dict, client,
             mount_path=None, max_sessions=None, events_path=None):
    """Run one bundle in manual-evaluation mode. Returns a single ``run_record``
    pointer dict (see module docstring). ``spec_md``/``event_log`` are accepted for
    signature stability but are not graded in this mode."""
    mount = mount_path or config.MOUNT_PATH
    max_sessions = max_sessions or config.DEFAULT_MAX_SESSIONS

    # 1. upload every uploaded Review 2 artifact (input + output) for mounting
    artifacts = [a for a in (env_record or {}).get("artifacts", [])
                 if a.get("uploaded") and a.get("file_ref")]
    file_ids, uploaded_ids = {}, []
    for a in artifacts:
        fid = client.upload_file(a["file_ref"])
        file_ids[os.path.basename(a["file_ref"])] = fid
        uploaded_ids.append(fid)

    # 2. create OR REUSE the agent (C6) + a cloud environment
    agent = client.create_or_reuse_agent(test_envelope_dict)
    if isinstance(agent, dict):
        agent_id = agent.get("id")
        agent_name = agent.get("name") or test_envelope_dict.get("name")
        agent_reused = bool(agent.get("reused"))
    else:                                            # tolerate a bare id from a stub
        agent_id, agent_name, agent_reused = agent, test_envelope_dict.get("name"), False
    environment_id = client.create_environment(config.ENVIRONMENT_DEFAULTS)

    # 3. one session per observed instance; send ONLY the trigger (P9). NO deletion —
    #    the session stays in the Console for the user to evaluate.
    resources = [{"file_id": fid, "mount_path": f"{mount.rstrip('/')}/{name}"}
                 for name, fid in file_ids.items()]
    triggers = generate_triggers(bundle, env_record, mount, max_sessions)
    sessions_out, records = [], []
    for t in triggers:
        sid = client.create_session(agent_id, environment_id, resources,
                                    title=f"Trace run — {bundle.get('id')}")

        try:
            events = client.send_trigger_and_collect(sid, t["message"])  # only the trigger
            status, error = _run_status(events)
        except Exception as e:
            events = [{"type": "client.error", "text": str(e), "name": None}]
            status, error = "errored", str(e)

        # persist the raw stream immediately (local backup; informational only)
        rec = {"trigger": t["message"], "session_id": sid,
               "status": status, "error": error, "events": events}
        records.append(rec)
        _persist_events(events_path, bundle, records)

        # list (do NOT download) the files the agent wrote under the outputs path
        try:
            output_files = client.list_output_files(sid, exclude_ids=uploaded_ids)
        except Exception:
            output_files = []
        rec["output_files"] = output_files
        _persist_events(events_path, bundle, records)

        sessions_out.append({
            "session_id": sid,
            "console_url": config.console_session_url(sid, agent_id),
            "console_hint": config.CONSOLE_SESSIONS_HINT,
            "status": status,
            "error": error,
            "output_files": output_files,
            "trigger": t["message"],
        })

    return {
        "bundle_id": bundle.get("id"),
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agent_reused": agent_reused,
        "environment_id": environment_id,
        "uploaded_file_ids": uploaded_ids,
        "sessions": sessions_out,
        "events_file": os.path.basename(events_path) if events_path else None,
        "ran_at": _now(),
    }


def _now():
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
