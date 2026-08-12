"""Spec 4 — the headless agent run path.

Reused by the Trace-side scheduler and the in-app "Run now" button so a deployed
agent runs WITHOUT the manual Managed Agents console step. It composes the
verified platform-client methods (all MA calls stay in ``api_client``): create a
fresh session bound to the customer's vault, send the trigger turn, and collect
the event stream. The approval policy is baked into the agent envelope at create
time, so a run honors it — this never bypasses outbound-write approvals.

Pure orchestration over an injected ``client``, so it runs offline against a stub
(mirrors ``deploy.py``).
"""

import datetime
import hashlib
import json
import re

from . import config, deploy

# event-type markers (substring match against the MA event stream)
_FAIL_MARKERS = ("failed", "error", "cancel", "timeout")
_APPROVAL_STOP = ("requires_action", "requires_approval", "tool_confirmation")


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def schedule_due(cadence, last_run_epoch, now_epoch):
    """Whether a schedule should fire now, given its cadence dict and the epoch of
    its last run (or None). Current cadences: {"kind":"daily","time":"HH:MM"} |
    {"kind":"weekly","weekday":N,"time":"HH:MM"} (N: 0=Mon … 6=Sun), local time.
    {"kind":"interval","minutes":N} and {"kind":"hourly"} are legacy (no longer
    offered in the UI) but still honored so any pre-existing schedule keeps firing."""
    if not cadence:
        return False
    kind = cadence.get("kind")
    if kind in ("interval", "hourly"):
        period = 3600 if kind == "hourly" else max(1, int(cadence.get("minutes", 15))) * 60
        return last_run_epoch is None or (now_epoch - last_run_epoch) >= period
    if kind == "daily":
        try:
            hh, mm = (int(x) for x in str(cadence.get("time", "09:00")).split(":"))
        except Exception:
            hh, mm = 9, 0
        now_dt = datetime.datetime.fromtimestamp(now_epoch)
        scheduled = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now_dt < scheduled:
            return False
        return last_run_epoch is None or datetime.datetime.fromtimestamp(last_run_epoch) < scheduled
    if kind == "weekly":
        try:
            hh, mm = (int(x) for x in str(cadence.get("time", "09:00")).split(":"))
        except Exception:
            hh, mm = 9, 0
        try:
            weekday = int(cadence.get("weekday", 0)) % 7      # 0=Mon … 6=Sun
        except Exception:
            weekday = 0
        now_dt = datetime.datetime.fromtimestamp(now_epoch)
        at_time = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # the most recent occurrence of weekday@time at or before now
        anchor = at_time - datetime.timedelta(days=(now_dt.weekday() - weekday) % 7)
        if now_dt < anchor:                                    # today is the day but before the time
            anchor -= datetime.timedelta(days=7)
        return last_run_epoch is None or datetime.datetime.fromtimestamp(last_run_epoch) < anchor
    return False


# --- Spec 6: derive a Trace-native run outcome from the session event stream ---
# Status is read from what actually happened, NOT from a raw count of tool calls.
# An always-allow agent's tool calls EXECUTE (no tool_confirmation), so they are
# completed actions — never "awaiting approval". "Needs approval" is reported only
# when the session genuinely stopped for input (stop_reason=requires_action).

def _action_labels(events):
    """Short, plain labels for the tool actions the agent actually took, in order
    (an executed outbound call surfaces as an ``mcp_tool_use`` event)."""
    out = []
    for ev in events:
        t = (ev.get("type") or "").lower()
        if "tool_use" in t and "confirmation" not in t and "result" not in t:
            out.append((ev.get("name") or ev.get("text") or "action").strip()[:120] or "action")
    return out


_SUMMARY_CHARS = 600


def _clip(text, limit=_SUMMARY_CHARS):
    """Trim to ``limit`` on a WORD boundary, marking the cut with an ellipsis.

    A hard slice cut mid-word ("Verification steps performed: 1. R"), which reads as a
    broken app rather than an abridged summary. 280 was also too tight for the
    verification narratives agents actually write; the activity view keeps more of each
    message than the feed summary does, so this only bounds the feed line.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,;:.") + "…"


def _last_text(events):
    for ev in reversed(events):
        text = (ev.get("text") or "").strip()
        if text:
            return _clip(text)
    return ""


def _failed(events):
    return any(any(k in (ev.get("type") or "").lower() for k in _FAIL_MARKERS) for ev in events)


def _needs_approval(events):
    """True only if the session genuinely stopped waiting for approval."""
    return any(any(k in (ev.get("stop_reason") or "").lower() for k in _APPROVAL_STOP)
               for ev in events)


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _content_hash(agent_id, kind, payload):
    """Stable id for one proposed action (Spec 8 idempotency): identical
    agent+kind+payload hash the same, so the same staged item is never duplicated
    across repeated scheduled runs."""
    blob = json.dumps({"a": agent_id, "k": kind, "p": payload},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _pending_items(agent_id, held, run_session_id):
    """Turn the platform's held actions into Trace-side staged-action records,
    capturing each action's FULL input so Trace can review and later execute it."""
    items = []
    for h in held:
        payload = h.get("input") or {}
        kind = h.get("kind") or "write"
        items.append({"action_id": h.get("action_id"),
                      "kind": kind,
                      "summary": h.get("summary") or kind,
                      "payload": payload,
                      "content_hash": _content_hash(agent_id, kind, payload),
                      "run_session_id": run_session_id})
    return items


# --- Spec 8 prompt-level staging ------------------------------------------
def _final_text(events):
    """The agent's full final message (untruncated), where the proposed-actions
    block lives — unlike _last_text which truncates for summaries."""
    for ev in reversed(events):
        t = (ev.get("text") or "").strip()
        if t:
            return t
    return ""


def _first_line(text, n=160):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:n]


def _app_label(slug):
    return str(slug or "").replace("_", " ").title()


def _staging_trigger(base_trigger, staged_slugs):
    """Prepend the staging directive (naming the staged apps) to the run's trigger so
    the agent reads freely but proposes — not performs — outbound actions in those apps."""
    apps = ", ".join(_app_label(s) for s in staged_slugs) or "the staged apps"
    # plain replace (not str.format) — the directive contains literal JSON braces
    directive = config.STAGING_DIRECTIVE.replace("{apps}", apps)
    return f"{directive}\nTask: {base_trigger}"


def _uncertainty_max_questions():
    """The cap on questions in one needs_input pause — mirrors the initial-setup
    interview's limit so both stages behave the same (per product decision)."""
    from .interview import _MAX_QUESTIONS
    return _MAX_QUESTIONS


def _uncertainty_trigger(base_trigger):
    """Append the uncertainty directive so the agent can pause on a genuinely blocking
    edge case (Gap #3). Applies to every run, staged or autonomous."""
    directive = config.UNCERTAINTY_DIRECTIVE.replace(
        "{max_questions}", str(_uncertainty_max_questions()))
    return f"{base_trigger}\n\n{directive}"


def _parse_needs_input(text):
    """Parse the agent's ``needs_input`` question array out of its final message. Requires
    the explicit ``needs_input`` fence (never a bare array — we don't want to misread a
    proposals block or other JSON as questions). Returns a list capped at the interview
    limit; [] when none/unparseable."""
    if not text:
        return []
    m = re.search(r"```needs_input\s*(\[.*?\])\s*```", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [q for q in data if isinstance(q, dict)][:_uncertainty_max_questions()]


def _looks_like_proposal(p):
    """Whether a parsed object is plausibly a staged action rather than stray prose.

    Agents wrap their answer in commentary — headings, findings tables, "let me now
    output the final actions". A loose parse can capture that narration as a proposal,
    and it then renders as an approve card made of paragraphs (observed 2026-08-11: a
    card whose summary was "The lessons_learned.md file has already been updated…").
    A summary is ONE line a human approves at a glance, so an essay in that field is the
    tell that this object is not an action.
    """
    if not isinstance(p, dict):
        return False
    summary = p.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 400:
        return False
    return bool(p.get("app") or p.get("action") or p.get("details"))


def _parse_proposals(text):
    """Parse the agent's ``proposed_actions`` JSON array out of its final message.
    Returns a list (possibly empty) on success, or None if no array could be parsed
    (the caller then falls back so a proposal is never silently dropped).

    Prefers the explicitly tagged fence, and among several takes the LAST — the model's
    final answer, not an earlier draft it talked itself out of. Items that don't look
    like actions are dropped rather than shown to a human as approvable.
    """
    if not text:
        return None
    candidates = [m.group(1) for m in
                  re.finditer(r"```proposed_actions\s*(.*?)```", text, re.DOTALL)]
    if not candidates:                                 # untagged or ```json fence
        candidates = [m.group(1) for m in
                      re.finditer(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)]
    if not candidates:                                 # no fence — bare top-level array
        m = re.search(r"(\[.*\])", text, re.DOTALL)
        candidates = [m.group(1)] if m else []
    for blob in reversed(candidates):                  # last block wins
        try:
            data = json.loads(blob.strip())
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            return [p for p in data if _looks_like_proposal(p)]
    return None


def _proposals_to_pending(agent_id, proposals, run_session_id):
    items = []
    for p in proposals:
        if not isinstance(p, dict):
            continue
        details = p.get("details")
        if not isinstance(details, (dict, list)):
            details = {"description": str(details)} if details is not None else {}
        kind = str(p.get("action") or p.get("app") or "action")
        summary = str(p.get("summary") or kind)
        rationale = p.get("rationale")
        rationale = str(rationale) if rationale is not None else ""
        items.append({"action_id": None, "kind": kind, "summary": summary,
                      "rationale": rationale,
                      "app": p.get("app") or "", "payload": details,
                      # content_hash keys on the action itself (agent+kind+details) so the
                      # same proposal dedups regardless of how the rationale is worded.
                      "content_hash": _content_hash(agent_id, kind, details),
                      "run_session_id": run_session_id})
    return items


def _memory_resources(agent):
    """The agent's memory-store attachment for a run session, or [] when the row has
    no store (legacy agents). Attached everywhere a session is created for the agent
    so memory stays consistent across scheduled runs and approval executions."""
    store_id = agent.get("memory_store_id")
    return [config.memory_resource(store_id)] if store_id else []


def _resume_preamble(resume):
    """Prepend the paused question + the human's answer so an auto-resumed run continues
    where it left off (Gap #3 B2). ``resume`` is ``{question, context, answer}``."""
    q = str(resume.get("question") or "").strip()
    ctx = str(resume.get("context") or "").strip()
    ans = str(resume.get("answer") or "").strip()
    return (f"You previously paused on this task and asked: \"{q}\".\n"
            + (f"What you'd found: {ctx}\n" if ctx else "")
            + f"The human has now answered: \"{ans}\".\n"
            "Use that answer to proceed and complete the task now.\n\nOriginal task: ")


def _guidance_preamble(lessons):
    """Lead the trigger with reusable lessons from past human decisions (Gap #3 B4) so the
    agent applies them instead of re-asking. ``lessons`` is a list of de-identified rules."""
    body = "\n".join(f"- {str(x).strip()}" for x in lessons if str(x).strip())
    return ("LESSONS FROM PAST HUMAN DECISIONS — apply these where they fit so you don't "
            "have to ask again:\n" + body + "\n\n") if body else ""


def _rejected_preamble(rejected):
    """Lead the trigger with actions the human REJECTED, so they aren't proposed again.

    Rejecting only set the row's status, which the agent never sees — so the next run
    re-proposed the identical action and rejecting taught the system nothing. A rejection
    is a signal ("don't do this"), and it has to reach the agent to act like one.
    ``rejected`` is a list of ``{summary, app}`` from recently rejected proposals.
    """
    lines = []
    for r in rejected or []:
        summary = str((r or {}).get("summary") or "").strip()
        if not summary:
            continue
        app = str((r or {}).get("app") or "").strip()
        lines.append(f"- {summary}" + (f"  [{app}]" if app else ""))
    if not lines:
        return ""
    return ("ALREADY REJECTED BY THE HUMAN — do NOT propose these again, and do not "
            "propose a lightly reworded version of them. If you believe one is now "
            "genuinely warranted because the situation changed, say why in its "
            "rationale rather than re-proposing it silently:\n"
            + "\n".join(lines[:20]) + "\n\n")


def run_once(client, agent, resume=None, guidance=None, rejected=None):
    """Run one agent once. ``agent`` is a registry row carrying ``agent_id``,
    ``vault_ref``, and ``trigger``. Returns a run record dict
    ``{agent_id, status, summary, details, run_session_id}`` where ``status`` is
    ``completed`` | ``needs_approval`` | ``needs_input`` | ``failed`` and ``details`` is
    the in-Trace list of actions. Never raises (a platform failure becomes ``failed``).
    ``resume`` (Gap #3) seeds the trigger with a prior paused question + human answer so
    the agent continues where it stopped. ``guidance`` (Gap #3 B4) is a list of retrieved
    reusable lessons injected as leading context. ``rejected`` is a list of
    ``{summary, app}`` the human turned down, so the same action isn't proposed again."""
    agent_id = agent.get("agent_id")
    base_trigger = agent.get("trigger") or "on a new matching task"
    if resume:
        base_trigger = _resume_preamble(resume) + base_trigger
    vault_ref = agent.get("vault_ref")
    staged = [s for s in (agent.get("staged_apps") or []) if s]
    trigger = _staging_trigger(base_trigger, staged) if staged else base_trigger
    trigger = _uncertainty_trigger(trigger)            # Gap #3: agent may pause when stuck
    if agent.get("memory_store_id"):
        trigger = trigger + "\n\n" + config.MEMORY_UPDATE_DIRECTIVE
    if rejected:                                       # don't re-propose what was refused
        trigger = _rejected_preamble(rejected) + trigger
    if guidance:                                       # Gap #3 B4: retrieved lessons up top
        trigger = _guidance_preamble(guidance) + trigger
    try:
        environment_id = client.create_environment()
        run_session_id = client.create_session(
            agent_id, environment_id, resources=_memory_resources(agent),
            title="Trace scheduled run",
            vault_ids=[vault_ref] if vault_ref else None)
    except Exception as e:                                  # noqa: BLE001
        return {"agent_id": agent_id, "status": "failed",
                "summary": f"Couldn't start the run: {type(e).__name__}", "details": [],
                "run_session_id": None}
    # The session now EXISTS — preserve its id no matter what happens next, so the
    # activity view can always read the real session (the prior bug discarded it).
    fallback = {"agent_id": agent_id, "status": "ran",
                "summary": "Run started — open Activity for what it did.",
                "details": [], "run_session_id": run_session_id}
    try:
        events = client.send_trigger_and_collect(run_session_id, trigger)
    except Exception:                                      # noqa: BLE001 — triggered; couldn't collect
        return fallback
    try:
        return _build_run_record(agent_id, events, run_session_id, client)
    except Exception:                                      # noqa: BLE001 — analysis must never raise
        return fallback


def _build_run_record(agent_id, events, run_session_id, client):
    """A coarse run record (failed/needs_approval/completed) for the run log. The
    authoritative per-session summary + staged proposals come from the activity engine
    (which reads the real session events); this is only a lightweight fallback label."""
    actions = _action_labels(events)
    if _failed(events):
        return {"agent_id": agent_id, "status": "failed",
                "summary": _last_text(events) or "The run stopped before finishing.",
                "details": actions, "run_session_id": run_session_id}
    questions = _parse_needs_input(_final_text(events))   # Gap #3: agent paused for input
    if questions:
        return {"agent_id": agent_id, "status": "needs_input",
                "summary": f"I need your input on {_plural(len(questions), 'thing')}.",
                "details": [str(q.get("question") or "") for q in questions],
                "questions": questions, "run_session_id": run_session_id}
    if _needs_approval(events):                        # always_allow shouldn't hold, but be safe
        try:
            held = deploy.list_held(client, run_session_id)
        except Exception:
            held = []
        pending = _pending_items(agent_id, held, run_session_id)
        labels = [(p.get("summary") or p.get("kind") or "action") for p in pending] or actions
        return {"agent_id": agent_id, "status": "needs_approval",
                "summary": f"Staged {_plural(len(labels), 'action')} for you to approve.",
                "details": labels, "pending": pending, "run_session_id": run_session_id}
    n = len(actions)
    summary = _last_text(events) or (f"Completed {_plural(n, 'action')}." if n else "Completed with no actions needed.")
    return {"agent_id": agent_id, "status": "completed",
            "summary": summary, "details": actions, "run_session_id": run_session_id}


def execute_pending(client, agent, pending):
    """Perform one previously-staged action on the user's approval (Spec 8 "quick fresh
    task"). Runs the deployed agent on a fresh session with an explicit EXECUTE
    instruction that overrides staging (staging lives only in the scheduled-run trigger,
    not the agent's system prompt, so a plain execute trigger performs the action under
    the always_allow toolsets). Returns ``{status: executed|failed, summary,
    run_session_id}``. Never raises."""
    agent_id = agent.get("agent_id")
    vault_ref = agent.get("vault_ref")
    summary = pending.get("summary") or pending.get("kind") or "the action"
    payload = pending.get("payload") or {}
    instruction = (
        "You are approved to perform this ONE action now, for real. Do NOT stage, "
        "propose, or describe it — actually carry it out using your tools, then stop. "
        f"Action: {summary}. Details: {json.dumps(payload, ensure_ascii=False, default=str)}")
    if agent.get("memory_store_id"):
        instruction = instruction + "\n\n" + config.MEMORY_UPDATE_DIRECTIVE
    try:
        environment_id = client.create_environment()
        run_session_id = client.create_session(
            agent_id, environment_id, resources=_memory_resources(agent),
            title="Trace approved action",
            vault_ids=[vault_ref] if vault_ref else None)
        events = client.send_trigger_and_collect(run_session_id, instruction)
    except Exception as e:                                  # noqa: BLE001
        return {"status": "failed", "summary": f"Couldn't perform it: {type(e).__name__}",
                "run_session_id": None}
    if _failed(events):
        return {"status": "failed",
                "summary": _last_text(events) or "The action stopped before finishing.",
                "run_session_id": run_session_id}
    return {"status": "executed",
            "summary": _last_text(events) or f"Performed {summary}.",
            "run_session_id": run_session_id}


def execute_actions(client, agent, items):
    """Perform several approved staged actions in ONE fresh session (batch approve from
    the activity screen). Explicit EXECUTE instruction overrides staging. Returns
    ``{status: executed|failed, summary, run_session_id}``. Never raises."""
    agent_id = agent.get("agent_id")
    vault_ref = agent.get("vault_ref")
    lines = []
    for i, it in enumerate(items, 1):
        summary = it.get("summary") or it.get("kind") or "action"
        details = json.dumps(it.get("payload") or {}, ensure_ascii=False, default=str)
        lines.append(f"{i}. {summary} — Details: {details}")
    instruction = (
        "You are approved to perform these actions now, for real. Do NOT stage, propose, "
        "or describe them — actually carry each one out using your tools, then stop:\n"
        + "\n".join(lines))
    if agent.get("memory_store_id"):
        instruction = instruction + "\n\n" + config.MEMORY_UPDATE_DIRECTIVE
    try:
        environment_id = client.create_environment()
        run_session_id = client.create_session(
            agent_id, environment_id, resources=_memory_resources(agent),
            title="Trace approved actions",
            vault_ids=[vault_ref] if vault_ref else None)
        events = client.send_trigger_and_collect(run_session_id, instruction)
    except Exception as e:                                  # noqa: BLE001
        return {"status": "failed", "summary": f"Couldn't perform them: {type(e).__name__}",
                "run_session_id": None}
    if _failed(events):
        return {"status": "failed",
                "summary": _last_text(events) or "Stopped before finishing.",
                "run_session_id": run_session_id}
    return {"status": "executed",
            "summary": _last_text(events) or f"Performed {_plural(len(items), 'action')}.",
            "run_session_id": run_session_id}
