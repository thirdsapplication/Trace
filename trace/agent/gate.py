"""Phase 3 — the hidden logic gate.

Runs an agent's TEST-variant config in a sealed sandbox with a synthesized trigger,
renders the FULL transcript (every step + every emitted-as-text outbound, in order),
and asks the walled-off grader to judge the agent's REASONING. Only agents whose
overall verdict is ``pass`` advance. This is hidden behind the "building…" state.

Unlike the manual-eval Deployer this uploads/mounts NOTHING — the trigger carries the
observed content inline, so the run is a pure reasoning test (no real files). It
reuses the Deployer's status + event-persistence helpers and the api_client session
machinery; the grader stays walled off (it only ever sees the rendered transcript).
"""

import datetime

from . import config, configgen, deployer, grader

# binary / oversized output handling for the transcript (evidence-fidelity fix #2)
_MAX_FILE_CHARS = 6000


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def generate_test_trigger(bundle, event_log, model_fn=None):
    """Synthesize the single, un-coached triggering event the agent would receive,
    from ONE observed instance: the bundle's own trigger text as the lead-in plus the
    first evidence event's observed detail as the concrete payload. Carries NO hints,
    step list, or criteria. ``model_fn`` (optional) may paraphrase into a clean
    trigger; when None (default/tests) the deterministic string is returned."""
    base = (bundle.get("trigger") or "").strip() or "A new instance of this workflow has arrived."
    _, observed_events, _ = configgen.make_engine_inputs(bundle, event_log)
    payload = ""
    for ev in observed_events:
        d = str(ev.get("description", "") or "").strip()
        if d:
            payload = d
            break
    trigger = f"{base}\n\n{payload}".strip() if payload else base
    if model_fn is not None:
        cleaned = (model_fn(_TRIGGER_SYSTEM, trigger) or "").strip()
        if cleaned:
            trigger = cleaned
    return trigger


_TRIGGER_SYSTEM = (
    "Rewrite the following into the single message an automation agent would actually "
    "receive when this workflow is triggered — a realistic, un-coached arrival in the "
    "worker's own voice/format. Keep all concrete observed content. Do NOT add any "
    "instructions, steps, hints, or commentary. Output only the message.")


def _looks_binary(s):
    if "\x00" in s:
        return True
    if not s:
        return False
    printable = sum(1 for c in s[:1000] if c.isprintable() or c in "\n\r\t ")
    return printable / min(len(s), 1000) < 0.7


def _render_output_file(name, content):
    content = content if isinstance(content, str) else str(content)
    if _looks_binary(content):
        return f"Output file {name}: [binary artifact, {len(content)} bytes — not rendered as text]"
    if len(content) > _MAX_FILE_CHARS:
        half = _MAX_FILE_CHARS // 2
        body = (content[:half] + f"\n…[truncated for grading — {len(content)} bytes total]…\n"
                + content[-half:])
        return f"Output file {name}:\n{body}"
    return f"Output file {name}:\n{content}"


def render_transcript(events, output_files=None):
    """Render normalized events ``[{type,text,name}]`` into an ordered, COMPLETE,
    untruncated transcript, then fold in any downloaded output files. Nothing is
    summarized or dropped — the grader must see everything (evidence-fidelity fix #1),
    including error/timeout markers (so it can return ``inconclusive``, not ``fail``)."""
    lines = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        et = (e.get("type") or "").strip()
        text = (e.get("text") or "").strip()
        if et == "agent.message":
            if text:
                lines.append(text)
        elif et == "agent.tool_use":
            name = (e.get("name") or "").strip()
            lines.append(f"[tool_use: {name}]" + (f" {text}" if text else ""))
        elif et in ("client.error", "client.timeout", "client.download_error"):
            lines.append(f"[error: {text or et}]")
        elif et in ("session.status_idle",) or "idle" in et or "ended" in et:
            lines.append("[run ended]")
        else:
            lines.append(f"[{et}] {text}".rstrip())
    transcript = "\n".join(lines)
    for name, content in (output_files or {}).items():
        transcript += "\n\n" + _render_output_file(name, content)
    return transcript


def run_gate(bundle, test_envelope_dict, criteria, event_log, client,
             judge_fn=None, trigger_fn=None, events_path=None):
    """Run the sealed-sandbox reasoning gate for one bundle and grade it. Returns a
    verdict record (see module docstring). The sandbox session is NOT deleted (kept for
    explicit cleanup). The grader only ever receives the rendered transcript."""
    bundle_id = bundle.get("id")
    trigger = (trigger_fn or generate_test_trigger)(bundle, event_log)

    agent = client.create_or_reuse_agent(test_envelope_dict)
    agent_id = agent.get("id") if isinstance(agent, dict) else agent
    environment_id = client.create_environment(config.ENVIRONMENT_DEFAULTS)
    sid = client.create_session(agent_id, environment_id, resources=[],
                                title=f"Trace gate — {bundle_id}")

    try:
        events = client.send_trigger_and_collect(sid, trigger)     # only the trigger (P9)
        status, error = deployer._run_status(events)
    except Exception as e:
        events = [{"type": "client.error", "text": str(e), "name": None}]
        status, error = "errored", str(e)

    try:
        output_files, _warnings = client.download_outputs(sid, exclude_ids=[])
    except Exception:
        output_files = {}

    # local backup of the raw stream (reuse the deployer's events-file convention)
    deployer._persist_events(events_path, bundle,
                             [{"trigger": trigger, "session_id": sid, "status": status,
                               "error": error, "events": events,
                               "output_files": list(output_files.keys())}])

    transcript = render_transcript(events, output_files)
    verdict = grader.grade(criteria, transcript, judge_fn=judge_fn)
    advanced = (verdict["overall"] == "pass" and status == "completed")

    return {
        "bundle_id": bundle_id,
        "overall": verdict["overall"],
        "per_criterion": verdict["per_criterion"],
        "notes": verdict["notes"],
        "trigger": trigger,
        "transcript": transcript,
        "session_id": sid,
        "status": status,
        "error": error,
        "advanced": advanced,
        "graded_at": _now(),
    }
