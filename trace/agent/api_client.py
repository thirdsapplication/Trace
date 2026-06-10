"""P11 — the ONE Managed Agents / Files API client.

Every platform call goes through this module, pinned to the beta version string, so
platform drift localizes here. The Managed Agents beta is young; the exact request
shapes below are best-effort against ``managed-agents-2026-04-01`` and MUST be
verified against the current docs (platform.claude.com/docs/en/managed-agents) at
run time — any divergence is a change to localize in THIS file, not across Stage 4.

The Deployer depends only on this interface (the public methods below), so the whole
of Stage 4 runs offline against a stub client in the test suite; real calls happen
only in the explicit gate run.
"""

from . import config

BETA_VERSION = config.BETA_VERSION


def _idof(obj):
    return getattr(obj, "id", obj.get("id") if isinstance(obj, dict) else None)


def _data(res):
    return getattr(res, "data", res)


class ManagedAgentsClient:
    """Thin wrapper over the Anthropic SDK's Managed Agents + Files beta. Methods
    return plain dicts/ids so the Deployer never touches SDK objects directly."""

    def __init__(self, api_key=None, beta=BETA_VERSION):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._beta_version = beta

    # extra headers carrying the beta opt-in on every call
    def _headers(self):
        return {"anthropic-beta": self._beta_version}

    def _files_ns(self):
        """The Files API namespace. On current SDKs it is ``client.beta.files``;
        older/newer ones expose ``client.files``. Probe both; fail with an actionable
        message so the fix stays localized to this file."""
        beta = getattr(self._client, "beta", None)
        if beta is not None and getattr(beta, "files", None) is not None:
            return beta.files
        if getattr(self._client, "files", None) is not None:
            return self._client.files
        raise RuntimeError(
            "this anthropic SDK exposes no Files API (tried client.beta.files and "
            "client.files). Upgrade `anthropic`, or wire the Files calls in "
            "trace/agent/api_client.py to the current API (platform.claude.com/docs).")

    def _agents_ns(self, name):
        """A Managed Agents beta namespace (``client.beta.<name>``). The beta is new
        and may not be typed in the installed SDK; raise a clear, localized error."""
        beta = getattr(self._client, "beta", None)
        ns = getattr(beta, name, None) if beta is not None else None
        if ns is None:
            raise RuntimeError(
                f"this anthropic SDK has no Managed Agents '{name}' API (client.beta.{name}). "
                "The Managed Agents beta is new — wire these calls in "
                "trace/agent/api_client.py to its current REST endpoints "
                "(platform.claude.com/docs/en/managed-agents). The compiled envelopes are "
                "unaffected; only the sandbox test run needs this.")
        return ns

    # --- Files API ---
    def upload_file(self, path):
        files = self._files_ns()
        creator = getattr(files, "create", None) or getattr(files, "upload", None)
        with open(path, "rb") as f:
            res = creator(file=f, extra_headers=self._headers())
        return _idof(res)

    def delete_file(self, file_id):
        try:
            self._files_ns().delete(file_id, extra_headers=self._headers())
        except Exception:
            pass

    def list_files(self):
        res = self._files_ns().list(extra_headers=self._headers())
        return [_idof(f) for f in _data(res)]

    # --- agents / environments / sessions (beta namespace; verify field names) ---
    def create_agent(self, envelope_dict):
        res = self._agents_ns("agents").create(extra_headers=self._headers(), **envelope_dict)
        return _idof(res)

    def create_environment(self, defaults=None):
        res = self._agents_ns("environments").create(
            extra_headers=self._headers(), **(defaults or config.ENVIRONMENT_DEFAULTS))
        return _idof(res)

    def create_session(self, agent_id, environment_id, file_ids, mount_path):
        res = self._agents_ns("sessions").create(
            extra_headers=self._headers(), agent_id=agent_id, environment_id=environment_id,
            files=[{"file_id": fid, "mount_path": mount_path} for fid in file_ids])
        return _idof(res)

    def send_message(self, session_id, message):
        # Only ever called with the trigger message (zero-coaching is enforced by the
        # Deployer, which never calls this again mid-session).
        return self._agents_ns("sessions").messages.create(
            session_id=session_id, extra_headers=self._headers(),
            messages=[{"role": "user", "content": message}])

    def get_events(self, session_id):
        res = self._agents_ns("sessions").events.list(session_id, extra_headers=self._headers())
        return [e if isinstance(e, dict) else getattr(e, "__dict__", {}) for e in _data(res)]

    def download_outputs(self, session_id):
        """Return ``{filename: text}`` for files the agent produced/modified."""
        out = {}
        files = self._files_ns()
        res = self._agents_ns("sessions").files.list(session_id, extra_headers=self._headers())
        for f in _data(res):
            fid = _idof(f)
            name = getattr(f, "name", None) or (f.get("name") if isinstance(f, dict) else None) or fid
            content = files.content(fid, extra_headers=self._headers())
            data = getattr(content, "text", None)
            if data is None:
                raw = getattr(content, "content", content)
                data = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            out[name] = data
        return out

    def delete_session(self, session_id):
        try:
            self._agents_ns("sessions").delete(session_id, extra_headers=self._headers())
        except Exception:
            pass

    def list_sessions(self):
        res = self._agents_ns("sessions").list(extra_headers=self._headers())
        return [_idof(s) for s in _data(res)]
