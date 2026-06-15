"""Phase 4 — the ONE Connection-Layer client (Pipedream Connect + Managed Agents vault).

Every platform call for connecting a worker's apps goes through this module so a future
swap (e.g. to Trace's own Google OAuth client) is a one-module change. It wraps TWO
external surfaces behind one interface; the orchestration in ``connect.py`` never sees
``anthropic`` or any Pipedream import.

CONFIRM AGAINST CURRENT DOCS — these are unverifiable in this environment:
  * Pipedream Connect (the per-``external_user_id`` account store: token creation and
    account lookup) is an external BETA API; every request/response shape below is a
    best-effort placeholder. Confirm before live use.
  * The Managed Agents vault calls use the verified ``managed-agents-2026-04-01`` SDK
    surface (``client.beta.vaults.*`` / ``client.beta.vaults.credentials.*``), but the
    *content* of the Pipedream credential written into the vault is unverifiable here.
    Note: the verified surface has no "bind a Pipedream connection to a vault" call —
    "bind" here means writing an MCP-keyed credential (keyed to the Pipedream MCP server
    URL) into the vault; a live session later attaches the vault via ``vault_ids``.

The client is injectable: ``connect.py`` and the UI take a client object, so all of
Phase 4 runs offline against a stub (see ``trace/tests/test_connect.py``).
"""

import json
import urllib.request

from . import config
from .api_client import _retry, _idof, _data, _attr

BETA_VERSION = config.BETA_VERSION


class ConnectClient:
    def __init__(self, api_key=None, beta=BETA_VERSION,
                 pipedream_project=None, connect_base=None):
        self._api_key = api_key
        self._beta_version = beta
        self._client = None                         # anthropic, built lazily (vault side)
        self._project = pipedream_project or config.PIPEDREAM_PROJECT
        self._connect_base = (connect_base or config.PIPEDREAM_CONNECT_BASE).rstrip("/")

    # --- Managed Agents vault side (lazy SDK; verified surface, see docstring) ---
    def _ma(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key,
                                               timeout=config.REQUEST_TIMEOUT,
                                               max_retries=config.RETRY_ATTEMPTS)
        return self._client

    def _vaults_ns(self):
        beta = getattr(self._ma(), "beta", None)
        ns = getattr(beta, "vaults", None) if beta is not None else None
        if ns is None:
            raise RuntimeError(
                "this anthropic SDK has no Managed Agents vaults API (client.beta.vaults). "
                "Run `pip install -U anthropic`. Connecting apps needs this; the compiled "
                "configs are unaffected.")
        return ns

    # --- Pipedream Connect side (stdlib HTTP; UNVERIFIABLE, confirm against docs) ---
    def _pd_request(self, method, path, body=None):
        url = f"{self._connect_base}/{path.lstrip('/')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key or ''}",       # confirm auth scheme
            "X-PD-Project": self._project,                          # confirm header/param
        })
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def create_connect_token(self, external_user_id, app):
        """Open the embedded Connect flow for this user+app. Returns
        ``{token, connect_url}`` — the UI hands ``connect_url`` to the Connect Link /
        SDK button. CONFIRM the endpoint + response shape against current docs."""
        out = _retry(lambda: self._pd_request("POST", "/tokens", {
            "external_user_id": external_user_id, "app": app}))
        return {"token": out.get("token"),
                "connect_url": out.get("connect_link_url") or out.get("connect_url")}

    def get_account(self, external_user_id, app):
        """Look up the account this user connected for ``app``, including its identity
        email (load-bearing for post-connect verification). CONFIRM against docs."""
        out = _retry(lambda: self._pd_request(
            "GET", f"/accounts?external_user_id={external_user_id}&app={app}"))
        accounts = out.get("accounts") or out.get("data") or []
        acc = accounts[0] if accounts else {}
        return {"connected": bool(acc),
                "account_id": acc.get("id") or acc.get("account_id"),
                "identity_email": (acc.get("identity") or {}).get("email")
                or acc.get("email")}

    def list_accounts(self, external_user_id):
        """All accounts connected for this customer (for display / isolation checks)."""
        out = _retry(lambda: self._pd_request(
            "GET", f"/accounts?external_user_id={external_user_id}"))
        return [{"app": a.get("app"), "account_id": a.get("id") or a.get("account_id"),
                 "identity_email": (a.get("identity") or {}).get("email") or a.get("email"),
                 "connected": True}
                for a in (out.get("accounts") or out.get("data") or [])]

    def ensure_vault(self, external_user_id):
        """Find-or-create the customer's vault (idiom mirrors create_or_reuse_agent).
        CONFIRM the vaults.create shape against current docs."""
        name = f"trace-connect-{external_user_id}"
        try:
            res = _retry(lambda: self._vaults_ns().list())
            for v in _data(res):
                if _attr(v, "name") == name:
                    return {"vault_ref": _idof(v), "created": False}
        except Exception:
            pass
        v = _retry(lambda: self._vaults_ns().create(name=name))
        return {"vault_ref": _idof(v), "created": True}

    def bind_connection(self, vault_ref, external_user_id, app, account=None):
        """Write the Pipedream connection into the vault as an MCP-keyed credential
        (keyed to the Pipedream MCP server URL). Idempotent: a credential already keyed
        to that URL is updated, not duplicated. CONFIRM the credential auth shape AND
        what material Pipedream Connect exposes against current docs (the biggest
        external unknown — see module docstring)."""
        creds = self._vaults_ns().credentials
        auth = {"type": "mcp_oauth", "mcp_server_url": config.PIPEDREAM_MCP["url"],
                "external_user_id": external_user_id, "app": app}       # confirm shape
        try:
            c = _retry(lambda: creds.create(vault_ref, auth=auth))
        except Exception as exc:                                # 409 duplicate -> update
            if "409" in str(exc) or "exist" in str(exc).lower():
                c = _retry(lambda: creds.update(vault_ref, auth=auth))
            else:
                raise
        return {"binding_ref": _idof(c) or f"{vault_ref}:{app}"}
