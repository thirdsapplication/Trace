"""P11 — the ONE Managed Agents / Files API client.

Every platform call goes through this module so platform drift localizes here. Wired
to the verified ``managed-agents-2026-04-01`` request shapes (docs retrieved
2026-06-10): the official Python SDK sets the beta header automatically on
``client.beta.*`` calls; the Files API takes a ``betas=[...]`` kwarg where the docs
show it.

If `client.beta.agents` / `client.beta.sessions` are missing, the installed SDK is
too old — `pip install -U anthropic` (the floor is pinned in requirements.txt).

The Deployer depends only on this interface, so all of Stage 4 runs offline against
a stub client; real calls happen only in the explicit gate run.
"""

import time
from pathlib import Path

from . import config

BETA_VERSION = config.BETA_VERSION


_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 529}
_RETRYABLE_TEXT = ("overloaded", "temporarily unavailable", "please retry", "try again",
                   "rate limit", "timeout", "timed out", "connection")


def _is_retryable(exc):
    """True for transient platform errors worth retrying (503 overloaded / file
    storage temporarily unavailable, 429, 5xx, connection blips)."""
    sc = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if sc in _RETRYABLE_STATUS:
        return True
    msg = str(exc).lower()
    return any(k in msg for k in _RETRYABLE_TEXT)


def _retry(fn, attempts=None, base=None, sleep=time.sleep):
    """Run ``fn`` with exponential backoff on transient errors. Non-retryable errors
    (auth, bad request) raise immediately; the last attempt's error propagates."""
    attempts = attempts or config.RETRY_ATTEMPTS
    base = base or config.RETRY_BASE_DELAY
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:                      # noqa: BLE001 (any SDK error)
            if not _is_retryable(exc) or i == attempts - 1:
                raise
            sleep(base * (2 ** i))


def _idof(obj):
    return getattr(obj, "id", obj.get("id") if isinstance(obj, dict) else None)


def _data(res):
    return getattr(res, "data", res)


def _attr(obj, name):
    """Read ``name`` from an SDK object or a dict, else None."""
    v = getattr(obj, name, None)
    if v is None and isinstance(obj, dict):
        v = obj.get(name)
    return v


def _select_downloadable(file_objs, exclude_ids, outputs_path):
    """Pick which session-scoped files to download as ``[(file_id, name)]``.

    Mounted inputs are READ-ONLY copies that the platform refuses to download
    ("file is not downloadable"); we therefore (1) drop any id we uploaded
    ourselves, (2) drop anything the SDK marks ``downloadable=False``, and
    (3) prefer files the agent wrote under the outputs path. Only when no file
    is under that path do we fall back to the remaining agent-created files."""
    exclude_ids = set(exclude_ids or ())
    candidates = []
    for f in file_objs:
        fid = _idof(f)
        if fid in exclude_ids:
            continue
        if _attr(f, "downloadable") is False:
            continue
        name = _attr(f, "filename") or fid
        path = _attr(f, "path") or _attr(f, "mount_path") or name or ""
        candidates.append((fid, name, str(path)))
    under_outputs = [(fid, name) for fid, name, path in candidates
                     if outputs_path and outputs_path in path]
    if under_outputs:
        return under_outputs
    return [(fid, name) for fid, name, _ in candidates]


def _content_text(content):
    """Normalize a downloaded file body to text."""
    data = getattr(content, "text", None)
    if data is None:
        read = getattr(content, "read", None)
        raw = read() if callable(read) else getattr(content, "content", content)
        data = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return data


def _event_dict(event):
    """Normalize a streamed event to ``{type, text, name}``."""
    et = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
    text, name = "", None
    content = getattr(event, "content", None)
    if content is None and isinstance(event, dict):
        content = event.get("content")
    if isinstance(content, list):
        text = " ".join(
            (getattr(b, "text", None) or (b.get("text") if isinstance(b, dict) else "") or "")
            for b in content).strip()
    elif isinstance(content, str):
        text = content
    name = getattr(event, "name", None) or (event.get("name") if isinstance(event, dict) else None)
    return {"type": et, "text": text, "name": name}


class ManagedAgentsClient:
    def __init__(self, api_key=None, beta=BETA_VERSION):
        import anthropic
        # per-request read timeout so a dead connection raises instead of blocking;
        # the SDK's own retry budget plus our _retry handle transient 503/overloaded.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=config.REQUEST_TIMEOUT,
                                           max_retries=config.RETRY_ATTEMPTS)
        self._beta_version = beta

    # --- namespace probes (clear, localized errors on SDK drift) ---
    def _files(self):
        beta = getattr(self._client, "beta", None)
        if beta is not None and getattr(beta, "files", None) is not None:
            return beta.files
        if getattr(self._client, "files", None) is not None:
            return self._client.files
        raise RuntimeError("this anthropic SDK exposes no Files API (client.beta.files). "
                           "Run `pip install -U anthropic`.")

    def _agents_ns(self, name):
        beta = getattr(self._client, "beta", None)
        ns = getattr(beta, name, None) if beta is not None else None
        if ns is None:
            raise RuntimeError(
                f"this anthropic SDK has no Managed Agents '{name}' API (client.beta.{name}). "
                "Run `pip install -U anthropic` to get the agent surface. The compiled "
                "envelopes are unaffected; only the sandbox test run needs this.")
        return ns

    # --- Files API ---
    def upload_file(self, path):
        # docs: client.beta.files.upload(file=Path(...)) -> file.id
        return _idof(_retry(lambda: self._files().upload(file=Path(path))))

    def delete_file(self, file_id):
        try:
            self._files().delete(file_id, betas=[self._beta_version])
        except TypeError:
            try:
                self._files().delete(file_id)
            except Exception:
                pass
        except Exception:
            pass

    def list_files(self):
        """Account-scoped file ids (for retention verification of uploads)."""
        res = _retry(lambda: self._files().list(betas=[self._beta_version]))
        return [_idof(f) for f in _data(res)]

    # --- agents / environments / sessions ---
    def create_agent(self, envelope_dict):
        # Pass only the documented create params (name, model, system, tools);
        # description/mcp_* live in the YAML, not the create call.
        a = _retry(lambda: self._agents_ns("agents").create(
            name=envelope_dict["name"], model=envelope_dict["model"],
            system=envelope_dict["system"], tools=envelope_dict["tools"]))
        return _idof(a)

    def _find_agent(self, name):
        """The first existing agent with this (Trace-deterministic) name, or None."""
        try:
            res = _retry(lambda: self._agents_ns("agents").list())
        except Exception:
            return None
        for a in _data(res):
            if _attr(a, "name") == name:
                return a
        return None

    def _try_update_agent(self, agent_id, envelope_dict):
        """Best-effort in-place update of an existing agent's definition. Returns
        True if the SDK supports it and the call succeeded, else False (reuse as-is).
        This is a cheap metadata call — no model tokens."""
        ns = self._agents_ns("agents")
        update = getattr(ns, "update", None)
        if not callable(update):
            return False
        try:
            _retry(lambda: update(agent_id, model=envelope_dict["model"],
                                  system=envelope_dict["system"], tools=envelope_dict["tools"]))
            return True
        except Exception:
            return False

    def create_or_reuse_agent(self, envelope_dict):
        """C6: agents have Trace-deterministic names, so an existing agent with the
        same name is REUSED (updated in place if the platform allows) rather than
        creating a duplicate every round. Returns ``{id, name, reused}``."""
        name = envelope_dict["name"]
        existing = self._find_agent(name)
        if existing is not None:
            agent_id = _idof(existing)
            self._try_update_agent(agent_id, envelope_dict)   # best-effort; reuse on failure
            return {"id": agent_id, "name": name, "reused": True}
        a = _retry(lambda: self._agents_ns("agents").create(
            name=name, model=envelope_dict["model"],
            system=envelope_dict["system"], tools=envelope_dict["tools"]))
        return {"id": _idof(a), "name": name, "reused": False}

    def create_environment(self, defaults=None):
        d = defaults or config.ENVIRONMENT_DEFAULTS
        return _idof(_retry(lambda: self._agents_ns("environments").create(
            name=d.get("name", "trace-test-env"), config=d.get("config", {}))))

    def create_session(self, agent_id, environment_id, resources, title=None, vault_ids=None):
        """``resources`` is a list of ``{file_id, mount_path}``. ``vault_ids`` (Phase 6)
        attaches the customer's bound vault so the LIVE agent reaches their real apps;
        omitted for sandbox runs. vault_ids is settable at create ONLY (not via update)."""
        kw = dict(agent=agent_id, environment_id=environment_id, title=title or "Trace test",
                  resources=[{"type": "file", "file_id": r["file_id"], "mount_path": r["mount_path"]}
                             for r in resources])
        if vault_ids:
            kw["vault_ids"] = list(vault_ids)              # the single vault-injection point
        return _idof(_retry(lambda: self._agents_ns("sessions").create(**kw)))

    def send_trigger_and_collect(self, session_id, trigger_text, timeout=None, max_events=None):
        """Send ONLY the trigger user.message and stream events until the agent
        finishes (a terminal event such as ``session.status_idle``). Bounded by a
        wall-clock and an event cap so it can never hang waiting on the platform.
        Never sends a second event — the zero-coaching rule (P9)."""
        timeout = timeout or config.DEFAULT_SESSION_TIMEOUT
        max_events = max_events or config.DEFAULT_MAX_EVENTS
        deadline = time.monotonic() + timeout
        sessions = self._agents_ns("sessions")
        events = []
        with sessions.events.stream(session_id) as stream:
            sessions.events.send(session_id, events=[
                {"type": "user.message", "content": [{"type": "text", "text": trigger_text}]}])
            for event in stream:
                ev = _event_dict(event)
                events.append(ev)
                et = ev["type"] or ""
                if any(k in et for k in ("idle", "completed", "failed", "ended", "done")):
                    break
                if len(events) >= max_events or time.monotonic() > deadline:
                    events.append({"type": "client.timeout", "name": None,
                                   "text": f"stopped after {timeout}s / {max_events} events "
                                           "without a terminal event"})
                    break
        return events

    def download_outputs(self, session_id, exclude_ids=None):
        """Download the agent's WRITTEN files for a session as
        ``({filename: text}, warnings)``.

        ``exclude_ids`` are the file ids we uploaded/mounted ourselves — those are
        read-only inputs the platform won't let us download, so they are filtered
        out before any download is attempted. Each remaining download is isolated:
        one un-downloadable file yields a per-file warning, never a failed run."""
        out, warnings = {}, []
        files = self._files()
        res = _retry(lambda: files.list(scope_id=session_id, betas=[self._beta_version]))
        for fid, name in _select_downloadable(_data(res), exclude_ids, config.OUTPUTS_PATH):
            try:
                content = _retry(lambda fid=fid: files.download(fid))
                out[name] = _content_text(content)
            except Exception as exc:                  # noqa: BLE001
                warnings.append({"file": name, "error": f"{type(exc).__name__}: {exc}"})
        return out, warnings

    def list_output_files(self, session_id, exclude_ids=None):
        """Names of the files the agent WROTE under the outputs path — a listing
        only, NO download (manual-evaluation mode reads outputs in the Console).
        Mounted inputs we uploaded are excluded; ``downloadable=False`` is honored."""
        files = self._files()
        res = _retry(lambda: files.list(scope_id=session_id, betas=[self._beta_version]))
        return [name for _fid, name
                in _select_downloadable(_data(res), exclude_ids, config.OUTPUTS_PATH)]

    def delete_session(self, session_id):
        try:
            self._agents_ns("sessions").delete(session_id)
        except Exception:
            pass

    def delete_agent(self, agent_id):
        self._agents_ns("agents").delete(agent_id)

    def list_sessions(self):
        res = _retry(lambda: self._agents_ns("sessions").list())
        return [_idof(s) for s in _data(res)]

    def ping(self):
        """Cheap connectivity + entitlement probe: list agents. Confirms the key works AND
        that the account has Managed Agents beta access (a plain messages call does not).
        Returns True or raises the underlying SDK error."""
        _retry(lambda: self._agents_ns("agents").list())
        return True

    # --- Phase 6: live deployment (start mode + approval-required outbound) ---
    # There is NO Managed Agents triggers/scheduler API and NO sessions.approvals API.
    # Start is owned by Trace (its own scheduler) or a Pipedream deployed trigger;
    # approval is the permission_policy {always_ask} on the toolset, resolved by replying
    # to the session with a single tool_confirmation event over the event stream.
    def set_trigger(self, agent_id, trigger=None, schedule=None):
        """Record how the LIVE agent starts. This makes NO platform call: a ``schedule``
        is handed to Trace's own scheduler (which calls ``create_session`` on cadence) and
        a ``trigger`` is registered as a Pipedream deployed trigger elsewhere. Returns the
        start descriptor."""
        return {"mode": "schedule" if schedule else "trigger",
                "value": schedule or trigger}

    def list_pending_approvals(self, session_id):
        """Outbound writes/sends the agent is holding for approval. A held call surfaces as
        the session going ``status_idle`` with ``stop_reason=requires_action`` and a pending
        ``agent.mcp_tool_use`` carrying a ``tool_use_id``. Returns
        ``[{action_id, kind, summary}]`` where ``action_id`` is that ``tool_use_id``."""
        sessions = self._agents_ns("sessions")
        res = _retry(lambda: sessions.events.list(session_id))
        out, resolved = [], set()
        for ev in _data(res):
            et = _attr(ev, "type") or ""
            tuid = _attr(ev, "tool_use_id")
            if "tool_confirmation" in et and tuid:        # a prior allow/deny already resolved it
                resolved.add(tuid)
        for ev in _data(res):
            et = _attr(ev, "type") or ""
            if "mcp_tool_use" not in et:
                continue
            tuid = _attr(ev, "tool_use_id") or _idof(ev)
            if not tuid or tuid in resolved:
                continue
            out.append({"action_id": tuid,
                        "kind": _attr(ev, "kind") or _attr(ev, "name") or "write",
                        "summary": _attr(ev, "summary") or _attr(ev, "name") or ""})
        return out

    def approve_action(self, session_id, action_id):
        """Commit one held outbound action: reply with a single ``tool_confirmation``
        ``allow`` event for its ``tool_use_id``."""
        return _retry(lambda: self._agents_ns("sessions").events.send(
            session_id, events=[{"type": "tool_confirmation",
                                 "tool_use_id": action_id, "result": "allow"}]))

    def reject_action(self, session_id, action_id, message=None):
        """Cancel one held outbound action: reply with a single ``tool_confirmation``
        ``deny`` event (the optional ``message`` is delivered to the agent so it can
        adjust)."""
        event = {"type": "tool_confirmation", "tool_use_id": action_id, "result": "deny"}
        if message:
            event["message"] = message
        return _retry(lambda: self._agents_ns("sessions").events.send(session_id, events=[event]))

    # --- explicit, human-initiated cleanup (C2) --------------------------
    def cleanup(self, session_ids=None, file_ids=None, agent_ids=None):
        """Delete the given Trace-created sessions, files, and agents. NEVER called
        automatically — only from the UI's 'Clean up platform' button. Resilient:
        each delete is isolated and the result reports exactly what was removed and
        what errored."""
        removed = {"sessions": [], "files": [], "agents": []}
        errors = []
        for sid in session_ids or []:
            try:
                _retry(lambda sid=sid: self._agents_ns("sessions").delete(sid))
                removed["sessions"].append(sid)
            except Exception as exc:                  # noqa: BLE001
                errors.append({"kind": "session", "id": sid, "error": f"{type(exc).__name__}: {exc}"})
        for fid in file_ids or []:
            try:
                self._delete_file_raw(fid)
                removed["files"].append(fid)
            except Exception as exc:                  # noqa: BLE001
                errors.append({"kind": "file", "id": fid, "error": f"{type(exc).__name__}: {exc}"})
        for aid in agent_ids or []:
            try:
                _retry(lambda aid=aid: self._agents_ns("agents").delete(aid))
                removed["agents"].append(aid)
            except Exception as exc:                  # noqa: BLE001
                errors.append({"kind": "agent", "id": aid, "error": f"{type(exc).__name__}: {exc}"})
        return {"removed": removed, "errors": errors}

    def _delete_file_raw(self, file_id):
        """Delete one file, raising on failure (so cleanup can report it)."""
        try:
            self._files().delete(file_id, betas=[self._beta_version])
        except TypeError:
            self._files().delete(file_id)
