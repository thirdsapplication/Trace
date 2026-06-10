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
    parts = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        role = e.get("role") or e.get("type") or ""
        text = e.get("text") or e.get("content") or ""
        if isinstance(text, list):
            text = " ".join(str(t.get("text", t) if isinstance(t, dict) else t) for t in text)
        parts.append(f"[{role}] {text}".strip())
    return "\n".join(p for p in parts if p)


def _detect_asked(events):
    """The agent asked the operator a question and produced no final result."""
    asked = any(isinstance(e, dict) and (e.get("type") == "question"
                or "?" in str(e.get("text", "")) and (e.get("role") in ("assistant", "agent")))
                for e in (events or []))
    return bool(asked)


def _detect_stalled(events, outputs):
    """No assistant output and no produced files => the agent stalled."""
    produced = bool(outputs)
    any_assistant = any(isinstance(e, dict) and e.get("role") in ("assistant", "agent")
                        and str(e.get("text", "")).strip() for e in (events or []))
    return not produced and not any_assistant


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
    triggers = generate_triggers(bundle, env_record, mount, max_sessions)
    instances, session_ids = [], []
    for t in triggers:
        sid = client.create_session(agent_id, environment_id, list(file_ids.values()), mount)
        session_ids.append(sid)
        client.send_message(sid, t["message"])      # the only message ever sent
        events = client.get_events(sid)
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
