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


class ManagedAgentsClient:
    """Thin wrapper over the Anthropic SDK's Managed Agents + Files beta. Methods
    return plain dicts/ids so the Deployer never touches SDK objects directly."""

    def __init__(self, api_key=None, beta=BETA_VERSION):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._beta = beta

    # extra headers carrying the beta opt-in on every call
    def _beta(self):
        return {"anthropic-beta": self._beta}

    # --- Files API ---
    def upload_file(self, path):
        with open(path, "rb") as f:
            res = self._client.files.create(file=f, extra_headers=self._beta())
        return getattr(res, "id", res.get("id") if isinstance(res, dict) else None)

    def delete_file(self, file_id):
        try:
            self._client.files.delete(file_id, extra_headers=self._beta())
        except Exception:
            pass

    def list_files(self):
        res = self._client.files.list(extra_headers=self._beta())
        return [getattr(f, "id", f.get("id")) for f in getattr(res, "data", res)]

    # --- agents / environments / sessions (beta namespace; verify field names) ---
    def create_agent(self, envelope_dict):
        res = self._client.beta.agents.create(extra_headers=self._beta(), **envelope_dict)
        return getattr(res, "id", res.get("id") if isinstance(res, dict) else None)

    def create_environment(self, defaults=None):
        res = self._client.beta.environments.create(
            extra_headers=self._beta(), **(defaults or config.ENVIRONMENT_DEFAULTS))
        return getattr(res, "id", res.get("id") if isinstance(res, dict) else None)

    def create_session(self, agent_id, environment_id, file_ids, mount_path):
        res = self._client.beta.sessions.create(
            extra_headers=self._beta(), agent_id=agent_id, environment_id=environment_id,
            files=[{"file_id": fid, "mount_path": mount_path} for fid in file_ids])
        return getattr(res, "id", res.get("id") if isinstance(res, dict) else None)

    def send_message(self, session_id, message):
        # Only ever called with the trigger message (zero-coaching is enforced by the
        # Deployer, which never calls this again mid-session).
        return self._client.beta.sessions.messages.create(
            session_id=session_id, extra_headers=self._beta(),
            messages=[{"role": "user", "content": message}])

    def get_events(self, session_id):
        res = self._client.beta.sessions.events.list(session_id, extra_headers=self._beta())
        return [e if isinstance(e, dict) else e.__dict__ for e in getattr(res, "data", res)]

    def download_outputs(self, session_id):
        """Return ``{filename: text}`` for files the agent produced/modified."""
        out = {}
        res = self._client.beta.sessions.files.list(session_id, extra_headers=self._beta())
        for f in getattr(res, "data", res):
            fid = getattr(f, "id", f.get("id"))
            name = getattr(f, "name", f.get("name", fid))
            content = self._client.files.content(fid, extra_headers=self._beta())
            data = getattr(content, "text", None)
            if data is None:
                raw = getattr(content, "content", content)
                data = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            out[name] = data
        return out

    def delete_session(self, session_id):
        try:
            self._client.beta.sessions.delete(session_id, extra_headers=self._beta())
        except Exception:
            pass

    def list_sessions(self):
        res = self._client.beta.sessions.list(extra_headers=self._beta())
        return [getattr(s, "id", s.get("id")) for s in getattr(res, "data", res)]
