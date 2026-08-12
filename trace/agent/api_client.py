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


def _full_event_dict(event):
    """Normalize a session event to the fuller shape the activity summarizer needs:
    ``{type, text, name, input, mcp_server_name, is_error, stop_reason, processed_at}``.
    Superset of ``_event_dict`` (so run-status helpers still work)."""
    d = _event_dict(event)
    for k in ("input", "mcp_server_name", "is_error", "processed_at",
              "tool_use_id", "mcp_tool_use_id", "agent_name"):
        d[k] = _attr(event, k)
    return d


def _has_terminal(events):
    """True once the session reached a terminal event (idle/completed/failed/ended)."""
    return any(any(k in (e.get("type") or "").lower()
                   for k in ("idle", "completed", "failed", "ended", "done"))
               for e in events)


def _event_dict(event):
    """Normalize a streamed event to ``{type, text, name, stop_reason}``."""
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
    # stop_reason distinguishes a completed idle from one held for approval
    # (requires_action). May sit on the event or its nested message/session.
    stop_reason = (getattr(event, "stop_reason", None)
                   or (event.get("stop_reason") if isinstance(event, dict) else None))
    if not stop_reason:
        nested = getattr(event, "message", None) or getattr(event, "session", None)
        stop_reason = getattr(nested, "stop_reason", None) if nested is not None else None
    if isinstance(stop_reason, dict):                  # real events nest it: {"type":"end_turn"}
        stop_reason = stop_reason.get("type")
    return {"type": et, "text": text, "name": name, "stop_reason": stop_reason}


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
        # Create params: name, model, system, tools, AND mcp_servers — the agent needs
        # its MCP servers registered (the finalized per-customer Pipedream servers), or
        # its mcp_toolset entries have no server backing them. (Beta shape — confirm
        # against current Managed Agents docs.)
        a = _retry(lambda: self._agents_ns("agents").create(
            name=envelope_dict["name"], model=envelope_dict["model"],
            system=envelope_dict["system"], tools=envelope_dict["tools"],
            mcp_servers=envelope_dict.get("mcp_servers") or []))
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

    def create_or_reuse_agent(self, envelope_dict):
        """C6: agents have Trace-deterministic names. If one with this name already exists,
        DELETE it and create it FRESH so a redeploy reflects the rebuilt config — Managed
        Agents 'update' is a versioning op we don't drive, and an in-place update can't be
        validated here. The delete is best-effort (swallowed) so deploy still proceeds; any
        straggler/duplicate is swept by the name-based cleanup. Recreating ends a session
        running on the old agent (acceptable for iterative redeploys).
        Returns ``{id, name, reused, recreated}``."""
        name = envelope_dict["name"]
        existing = self._find_agent(name)
        recreated = False
        if existing is not None:
            old_id = _idof(existing)
            if old_id:
                try:
                    self.delete_agent(old_id)
                except Exception:                     # noqa: BLE001 — don't block the redeploy
                    pass
                recreated = True
        a = _retry(lambda: self._agents_ns("agents").create(
            name=name, model=envelope_dict["model"],
            system=envelope_dict["system"], tools=envelope_dict["tools"],
            mcp_servers=envelope_dict.get("mcp_servers") or []))
        return {"id": _idof(a), "name": name, "reused": False, "recreated": recreated}

    def create_environment(self, defaults=None):
        d = defaults or config.ENVIRONMENT_DEFAULTS
        return _idof(_retry(lambda: self._agents_ns("environments").create(
            name=d.get("name", "trace-test-env"), config=d.get("config", {}))))

    def create_session(self, agent_id, environment_id, resources, title=None, vault_ids=None):
        """``resources`` is a list of ``{file_id, mount_path}`` file mounts, or resource
        dicts that already carry a ``type`` (e.g. a ``memory_store`` attachment from
        ``config.memory_resource``) which pass through unchanged. ``vault_ids`` (Phase 6)
        attaches the customer's bound vault so the LIVE agent reaches their real apps;
        omitted for sandbox runs. vault_ids is settable at create ONLY (not via update)."""
        kw = dict(agent=agent_id, environment_id=environment_id, title=title or "Trace test",
                  resources=[r if r.get("type")
                             else {"type": "file", "file_id": r["file_id"],
                                   "mount_path": r["mount_path"]}
                             for r in resources])
        if vault_ids:
            kw["vault_ids"] = list(vault_ids)              # the single vault-injection point
        return _idof(_retry(lambda: self._agents_ns("sessions").create(**kw)))

    def list_session_events(self, session_id):
        """The full event list for a session (the reliable read — ``events.list``, the
        same call ``list_pending_approvals`` uses), normalized for the summarizer."""
        res = _retry(lambda: self._agents_ns("sessions").events.list(session_id))
        return [_full_event_dict(e) for e in _data(res)]

    def send_trigger_and_collect(self, session_id, trigger_text, timeout=None, max_events=None):
        """Send ONLY the trigger user.message, then POLL the session's event list until
        a terminal event or a wall-clock/event-count bound. Uses ``events.list`` (which
        is reliable) rather than ``events.stream`` (which the platform can reject with a
        non-retryable 400). Never sends a second event — the zero-coaching rule (P9)."""
        timeout = timeout or config.DEFAULT_SESSION_TIMEOUT
        max_events = max_events or config.DEFAULT_MAX_EVENTS
        poll = getattr(config, "SESSION_POLL_SECONDS", 3)
        deadline = time.monotonic() + timeout
        sessions = self._agents_ns("sessions")
        _retry(lambda: sessions.events.send(session_id, events=[
            {"type": "user.message", "content": [{"type": "text", "text": trigger_text}]}]))
        while True:
            try:
                events = self.list_session_events(session_id)
            except Exception:                                  # transient read — keep waiting
                events = []
            if _has_terminal(events):
                return events
            if len(events) >= max_events or time.monotonic() > deadline:
                return events + [{"type": "client.timeout", "name": None,
                                  "text": f"stopped after {timeout}s / {max_events} events "
                                          "without a terminal event"}]
            time.sleep(poll)

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

    # --- Phase 6: live deployment ---------------------------------------
    # Start is now platform-native: Managed Agents DEPLOYMENTS (manual `run` or cron
    # schedule) create the run sessions; deployment_runs.list is how Trace learns about
    # platform-initiated sessions. Approval remains the permission_policy {always_ask}
    # on a toolset, resolved by replying with a single tool_confirmation event.
    def set_trigger(self, agent_id, trigger=None, schedule=None):
        """DEPRECATED legacy no-op kept for the pre-deployment agent path. Makes NO
        platform call; returns the start descriptor. New deploys use
        ``create_deployment`` instead."""
        return {"mode": "schedule" if schedule else "trigger",
                "value": schedule or trigger}

    # --- memory stores (agent memory beta) --------------------------------
    # Beta shape — confirm against current Managed Agents memory docs: stores are
    # created once, attached at session/deployment create via a memory_store resource,
    # and mount at /mnt/memory/<store-name>/ inside the container.
    def create_or_reuse_memory_store(self, name, description, documents=None):
        """One memory store per USER, keyed by its Trace-deterministic name. REUSED
        when it already exists (unlike agents, which are recreated on redeploy) so
        accumulated lessons survive redeploys — and so a user's second agent joins
        the store their first agent created. ``documents`` is the ``(path, content)``
        seed list for the calling agent (defaults to the legacy root-level pair);
        seeds run on BOTH the fresh and the reused path, because on a shared store
        "reused" only means another agent got there first — this agent's own
        documents may not exist yet. ``seed_memory`` tolerates already-existing
        paths (a reused document keeps its content); any other seed failure raises
        (fail loud — a half-initialized store is worse than a failed deploy).
        Returns ``{id, reused}``."""
        documents = documents or [(config.MEMORY_STATUS_DOC, config.MEMORY_STATUS_SEED),
                                  (config.MEMORY_LESSONS_DOC, config.MEMORY_LESSONS_SEED)]
        stores = self._agents_ns("memory_stores")
        store_id, reused = None, False
        try:
            res = _retry(lambda: stores.list())
            for s in _data(res):
                if _attr(s, "name") == name:
                    store_id, reused = _idof(s), True
                    break
        except Exception:                                  # noqa: BLE001 — list is best-effort
            pass
        if store_id is None:
            store = _retry(lambda: stores.create(name=name, description=description))
            store_id = _idof(store)
        for path, content in documents:
            self.seed_memory(store_id, path, content)
        return {"id": store_id, "reused": reused}

    def seed_memory(self, store_id, path, content):
        """Create one memory document at ``path``. Tolerates the document already
        existing (a reused store keeps its content); other errors raise."""
        try:
            return _retry(lambda: self._agents_ns("memory_stores").memories.create(
                store_id, path=path, content=content))
        except Exception as exc:                          # noqa: BLE001
            msg = str(exc).lower()
            if "conflict" in msg or "already exists" in msg:
                return None
            raise

    def delete_memory_store(self, store_id):
        """Best-effort store deletion — used only by explicit cleanup."""
        try:
            _retry(lambda: self._agents_ns("memory_stores").delete(store_id))
            return True
        except Exception:                                  # noqa: BLE001
            return False

    # --- deployments (platform-native start) ------------------------------
    # Beta shape — confirm against current Managed Agents docs: a deployment binds an
    # agent + environment + initial_events (the per-run message set) + optional cron
    # schedule; each trigger (cron or manual `run`) creates a deployment_run carrying
    # the session_id. Deployments are immutable — a schedule change is archive+create.
    def create_deployment(self, name, agent_id, environment_id, trigger_text,
                          schedule=None, resources=None, vault_ids=None, metadata=None):
        """Create one deployment whose every run starts from ``trigger_text`` (the
        initial user.message). ``schedule`` is ``{expression, timezone}`` for cron or
        None for a manual deployment. NO exception swallowing — a wrong shape must
        surface the real API error (the `always_ask` lesson)."""
        kw = dict(name=name, agent=agent_id, environment_id=environment_id,
                  initial_events=[{"type": "user.message",
                                   "content": [{"type": "text", "text": trigger_text}]}])
        if schedule:
            kw["schedule"] = {"type": "cron", "expression": schedule["expression"],
                              "timezone": schedule["timezone"]}
        if resources:
            kw["resources"] = list(resources)
        if vault_ids:
            kw["vault_ids"] = list(vault_ids)
        if metadata:
            kw["metadata"] = dict(metadata)
        # NOT wrapped in _retry: create is side-effecting and has no idempotency key,
        # so a retry after a response-phase timeout would create a SECOND live cron
        # deployment. A transient failure surfaces to the caller (schedule -> 502,
        # deploy -> per-bundle error), which the user retries safely.
        return _idof(self._agents_ns("deployments").create(**kw))

    def _drun_dict(self, run):
        """Normalize one deployment_run to ``{id, session_id, error_type,
        error_message, created_at}`` (error is nested as error.type/error.message)."""
        err = _attr(run, "error")
        return {"id": _idof(run),
                "session_id": _attr(run, "session_id"),
                "error_type": _attr(err, "type") if err is not None else None,
                "error_message": _attr(err, "message") if err is not None else None,
                "created_at": _attr(run, "created_at")}

    def run_deployment(self, deployment_id):
        """Trigger one manual run of a deployment; returns the normalized
        deployment_run (carrying the new session_id, or an error). NOT wrapped in
        _retry: a retried run would execute the agent's workflow (real outbound
        actions) twice. A transient failure is recorded as a failed run instead."""
        return self._drun_dict(self._agents_ns("deployments").run(deployment_id))

    def pause_deployment(self, deployment_id):
        _retry(lambda: self._agents_ns("deployments").pause(deployment_id))
        return True

    def unpause_deployment(self, deployment_id):
        _retry(lambda: self._agents_ns("deployments").unpause(deployment_id))
        return True

    def archive_deployment(self, deployment_id):
        """Archive (terminal). Tolerant of already-archived/missing deployments —
        used at schedule changes and cleanup, where a stale id must not block."""
        try:
            _retry(lambda: self._agents_ns("deployments").archive(deployment_id))
            return True
        except Exception:                                  # noqa: BLE001
            return False

    def list_deployment_runs(self, deployment_id):
        """Every run of one deployment (scheduled + manual), normalized — how Trace
        learns about platform-initiated sessions."""
        res = _retry(lambda: self._agents_ns("deployment_runs").list(
            deployment_id=deployment_id))
        return [self._drun_dict(r) for r in _data(res)]

    def list_pending_approvals(self, session_id):
        """Outbound writes/sends the agent is holding for approval. A held call surfaces as
        the session going ``status_idle`` with ``stop_reason=requires_action`` and a pending
        ``agent.mcp_tool_use`` carrying a ``tool_use_id``. Returns
        ``[{action_id, kind, summary, input}]`` where ``action_id`` is that ``tool_use_id``
        and ``input`` is the tool's full arguments (recipient/subject/body, or the write
        target+values) — the fully-specified action Trace stages for review (Spec 8)."""
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
            tool_input = _attr(ev, "input") or _attr(ev, "arguments") or {}
            out.append({"action_id": tuid,
                        "kind": _attr(ev, "kind") or _attr(ev, "name") or "write",
                        "summary": _attr(ev, "summary") or _attr(ev, "name") or "",
                        "input": tool_input})
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
    def _agent_ids_by_name(self, names):
        """Ids of every existing agent whose name is in ``names`` — used to sweep
        Trace-created agents (current + orphaned stale/duplicate ones from earlier
        rounds) that share this session's workflow names."""
        names = set(n for n in (names or []) if n)
        if not names:
            return []
        try:
            res = _retry(lambda: self._agents_ns("agents").list())
        except Exception:
            return []
        out = []
        for a in _data(res):
            if _attr(a, "name") in names:
                aid = _idof(a)
                if aid:
                    out.append(aid)
        return out

    def cleanup(self, session_ids=None, file_ids=None, agent_ids=None, agent_names=None,
                deployment_ids=None, memory_store_ids=None):
        """Delete the given Trace-created sessions, files, agents, deployments, and
        memory stores. NEVER called automatically — only from the UI's 'Clean up
        platform' button. ``agent_names`` additionally sweeps any agents the platform
        still has under this session's workflow names (orphaned/stale duplicates not in
        the records). Resilient: each delete is isolated and the result reports exactly
        what was removed and errored."""
        agent_ids = list(agent_ids or [])
        for aid in self._agent_ids_by_name(agent_names):
            if aid not in agent_ids:
                agent_ids.append(aid)
        removed = {"sessions": [], "files": [], "agents": [],
                   "deployments": [], "memory_stores": []}
        errors = []
        for did in deployment_ids or []:
            try:
                if self.archive_deployment(did):
                    removed["deployments"].append(did)
            except Exception as exc:                  # noqa: BLE001
                errors.append({"kind": "deployment", "id": did,
                               "error": f"{type(exc).__name__}: {exc}"})
        for mid in memory_store_ids or []:
            try:
                if self.delete_memory_store(mid):
                    removed["memory_stores"].append(mid)
            except Exception as exc:                  # noqa: BLE001
                errors.append({"kind": "memory_store", "id": mid,
                               "error": f"{type(exc).__name__}: {exc}"})
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
