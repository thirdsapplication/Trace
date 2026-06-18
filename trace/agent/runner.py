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

from . import config, deploy

_TERMINAL_FAIL = ("failed", "error", "timeout", "cancel")


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def schedule_due(cadence, last_run_epoch, now_epoch):
    """Whether a schedule should fire now, given its cadence dict and the epoch of
    its last run (or None). Cadences: {"kind":"interval","minutes":N} |
    {"kind":"hourly"} | {"kind":"daily","time":"HH:MM"} (local time)."""
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
    return False


def _status(events, held):
    if held:
        return "halted"
    for ev in events:
        if any(k in (ev.get("type") or "").lower() for k in _TERMINAL_FAIL):
            return "failed"
    return "clean"


def _summary(events, held, status):
    if status == "halted":
        return f"{len(held)} action(s) awaiting your approval"
    # last non-empty text the agent produced, else a terminal/status note
    for ev in reversed(events):
        text = (ev.get("text") or "").strip()
        if text:
            return text[:280]
    if status == "failed":
        return "Run failed before completing."
    return f"Completed ({len(events)} events)."


def run_once(client, agent):
    """Run one agent once. ``agent`` is a registry row carrying ``agent_id``,
    ``vault_ref``, and ``trigger``. Returns a run record dict
    ``{agent_id, status, summary, run_session_id, console_url}``; never raises
    (a platform failure becomes a ``failed`` record so the activity view shows it).
    """
    agent_id = agent.get("agent_id")
    console_url = config.console_url() if hasattr(config, "console_url") else config.CONSOLE_BASE
    trigger = agent.get("trigger") or "on a new matching task"
    vault_ref = agent.get("vault_ref")
    try:
        environment_id = client.create_environment()
        run_session_id = client.create_session(
            agent_id, environment_id, resources=[], title="Trace scheduled run",
            vault_ids=[vault_ref] if vault_ref else None)
        events = client.send_trigger_and_collect(run_session_id, trigger)
        held = deploy.list_held(client, run_session_id)
        status = _status(events, held)
        return {"agent_id": agent_id, "status": status,
                "summary": _summary(events, held, status),
                "run_session_id": run_session_id, "console_url": console_url}
    except Exception as e:                                  # noqa: BLE001
        return {"agent_id": agent_id, "status": "failed",
                "summary": f"{type(e).__name__}: {e}",
                "run_session_id": None, "console_url": console_url}
