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


def _idof(obj):
    return getattr(obj, "id", obj.get("id") if isinstance(obj, dict) else None)


def _data(res):
    return getattr(res, "data", res)


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
        # per-request read timeout so a dead connection raises instead of blocking
        self._client = anthropic.Anthropic(api_key=api_key, timeout=config.REQUEST_TIMEOUT)
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
        return _idof(self._files().upload(file=Path(path)))

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
        res = self._files().list(betas=[self._beta_version])
        return [_idof(f) for f in _data(res)]

    # --- agents / environments / sessions ---
    def create_agent(self, envelope_dict):
        # Pass only the documented create params (name, model, system, tools);
        # description/mcp_* live in the YAML, not the create call.
        a = self._agents_ns("agents").create(
            name=envelope_dict["name"], model=envelope_dict["model"],
            system=envelope_dict["system"], tools=envelope_dict["tools"])
        return _idof(a)

    def create_environment(self, defaults=None):
        d = defaults or config.ENVIRONMENT_DEFAULTS
        return _idof(self._agents_ns("environments").create(
            name=d.get("name", "trace-test-env"), config=d.get("config", {})))

    def create_session(self, agent_id, environment_id, resources, title=None):
        """``resources`` is a list of ``{file_id, mount_path}``."""
        return _idof(self._agents_ns("sessions").create(
            agent=agent_id, environment_id=environment_id,
            title=title or "Trace test",
            resources=[{"type": "file", "file_id": r["file_id"], "mount_path": r["mount_path"]}
                       for r in resources]))

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

    def download_outputs(self, session_id):
        """List session-scoped files (the agent's WRITTEN copies — mounted files are
        read-only) and download them as ``{filename: text}``."""
        out = {}
        files = self._files()
        res = files.list(scope_id=session_id, betas=[self._beta_version])
        for f in _data(res):
            fid = _idof(f)
            name = (getattr(f, "filename", None)
                    or (f.get("filename") if isinstance(f, dict) else None) or fid)
            content = files.download(fid)
            data = getattr(content, "text", None)
            if data is None:
                read = getattr(content, "read", None)
                raw = read() if callable(read) else getattr(content, "content", content)
                data = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            out[name] = data
        return out

    def delete_session(self, session_id):
        try:
            self._agents_ns("sessions").delete(session_id)
        except Exception:
            pass

    def list_sessions(self):
        res = self._agents_ns("sessions").list()
        return [_idof(s) for s in _data(res)]
