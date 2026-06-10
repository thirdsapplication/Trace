"""Component B — the Test Deployer (P6–P9).

Orchestrates a full sandbox test run through the injected API client: upload the
Review 2 artifact files, create/reuse the agent + a cloud environment, create a
session with files mounted, send ONLY the trigger message(s), capture the event
stream, download outputs, then delete the session and uploaded files (nothing
persists server-side after a test). It never sends a steering message (zero
coaching, P9). The run is graded against the spec's own acceptance criteria (P8).

The ``client`` is injected so the whole flow runs offline against a stub.
"""

import os

from . import config, grader
from .trigger import generate_triggers


def _events_to_transcript(events):
    """Render normalized events ({type, text, name}) to a transcript. Event types:
    user.message (our trigger), agent.message (text blocks), agent.tool_use (.name),
    session.status_idle (terminal)."""
    parts = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        et = e.get("type") or ""
        if et == "agent.tool_use":
            parts.append(f"[tool_use] {e.get('name', '')}".strip())
        else:
            text = (e.get("text") or "").strip()
            if text:
                parts.append(f"[{et}] {text}")
    return "\n".join(parts)


def _detect_asked(events):
    """The agent posed a question to the operator (an agent.message containing '?')."""
    return any(isinstance(e, dict) and e.get("type") == "agent.message"
               and "?" in str(e.get("text", "")) for e in (events or []))


def _detect_stalled(events, outputs):
    """No agent text output and no produced files => the agent stalled."""
    produced = bool(outputs)
    any_agent_text = any(isinstance(e, dict) and e.get("type") == "agent.message"
                         and str(e.get("text", "")).strip() for e in (events or []))
    return not produced and not any_agent_text


def run_test(bundle, spec_md, env_record, event_log, test_envelope_dict, client,
             judge_fn=None, mount_path=None, max_sessions=None):
    """Run the sandbox test for one bundle. Returns ``(report, artifacts)`` where
    ``report`` is the ``test_report`` dict and ``artifacts`` carries downloaded output
    files for the UI to save."""
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

    # 2. agent + cloud environment (limited networking, package install available)
    agent_id = client.create_agent(test_envelope_dict)
    environment_id = client.create_environment(config.ENVIRONMENT_DEFAULTS)

    # 3–6. one session per observed instance; send ONLY the trigger (P9)
    resources = [{"file_id": fid, "mount_path": f"{mount.rstrip('/')}/{name}"}
                 for name, fid in file_ids.items()]
    triggers = generate_triggers(bundle, env_record, mount, max_sessions)
    instances, session_ids = [], []
    for t in triggers:
        sid = client.create_session(agent_id, environment_id, resources,
                                    title=f"Trace test — {bundle.get('id')}")
        session_ids.append(sid)
        events = client.send_trigger_and_collect(sid, t["message"])  # only the trigger
        outputs = client.download_outputs(sid)
        instances.append({
            "trigger": t["message"],
            "transcript": _events_to_transcript(events),
            "output_files": outputs,
            "asked_question": _detect_asked(events),
            "stalled": _detect_stalled(events, outputs),
        })
        client.delete_session(sid)                   # 7. retention: nothing persists

    for fid in uploaded_ids:
        client.delete_file(fid)

    # retention verification (acceptance #4): nothing of ours remains server-side
    try:
        remaining_sessions = [s for s in client.list_sessions() if s in session_ids]
        remaining_files = [f for f in client.list_files() if f in uploaded_ids]
        retention_clean = not remaining_sessions and not remaining_files
    except Exception:
        retention_clean = None

    combined = {
        "transcript": "\n\n=== NEXT INSTANCE ===\n\n".join(i["transcript"] for i in instances),
        "output_files": {k: v for i in instances for k, v in i["output_files"].items()},
        "stalled": any(i["stalled"] for i in instances),
        "asked_question": any(i["asked_question"] for i in instances),
    }
    criteria = grader.parse_acceptance_criteria(spec_md)
    report = grader.grade(criteria, combined, judge_fn)
    report.update({
        "bundle_id": bundle.get("id"),
        "sessions_run": len(instances),
        "retention_clean": retention_clean,
        "instances": [{"trigger": i["trigger"], "stalled": i["stalled"],
                       "asked_question": i["asked_question"],
                       "output_files": list(i["output_files"].keys())} for i in instances],
    })
    return report, {"output_files": combined["output_files"]}
