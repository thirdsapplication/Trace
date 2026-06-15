"""Phase 4 — the ONE Connection-Layer client (Pipedream Connect + Managed Agents vault).

Every platform call for connecting a worker's apps goes through this module so a future
swap (e.g. to Trace's own Google OAuth client) is a one-module change. It wraps TWO
external surfaces behind one interface; the orchestration in ``connect.py`` never sees
``anthropic`` or any Pipedream import.

Shapes are doc-confirmed (Verified Platform Reference, June 2026) but the calls cannot be
exercised offline in this environment:
  * Pipedream Connect (the per-``external_user_id`` account store) is project-scoped under
    ``/v1/connect/{project_id}/…``; the bearer is Trace's project-scoped developer token
    (client_credentials, 1h TTL), and ``external_user_id`` + ``app`` ride in the MCP URL.
  * The Managed Agents vault calls use the verified ``managed-agents-2026-04-01`` SDK
    surface (``client.beta.vaults.*`` / ``client.beta.vaults.credentials.*``). There is no
    "bind a Pipedream connection to a vault" call — "bind" means writing an ``mcp_oauth``
    credential (keyed to the per-customer Pipedream MCP server URL) into the vault; a live
    session later attaches the vault via ``vault_ids``.

The client is injectable: ``connect.py`` and the UI take a client object, so all of
Phase 4 runs offline against a stub (see ``trace/tests/test_connect.py``).
"""

import json
import time
import urllib.parse
import urllib.request

from . import config
from .api_client import _retry, _idof, _data, _attr

BETA_VERSION = config.BETA_VERSION


class ConnectClient:
    def __init__(self, api_key=None, beta=BETA_VERSION,
                 pipedream_project=None, connect_base=None,
                 client_id=None, client_secret=None, environment=None):
        self._api_key = api_key                     # anthropic key (vault side only)
        self._beta_version = beta
        self._client = None                         # anthropic, built lazily (vault side)
        self._project = pipedream_project or config.PIPEDREAM_PROJECT
        self._connect_base = (connect_base or config.PIPEDREAM_CONNECT_BASE).rstrip("/")
        self._client_id = client_id or config.PIPEDREAM_CLIENT_ID
        self._client_secret = client_secret or config.PIPEDREAM_CLIENT_SECRET
        self._environment = environment or config.PIPEDREAM_ENVIRONMENT
        self._pd_token = None                       # cached Pipedream dev token
        self._pd_token_exp = 0.0                    # epoch seconds

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

    # --- Pipedream Connect side (stdlib HTTP) ---
    # Bearer is Trace's PROJECT-SCOPED developer token (client_credentials), NOT the
    # anthropic key — the same token authenticates every Pipedream URL because the
    # customer + app ride in the URL, not the token. Paths are project-scoped under
    # /connect/{project_id}/...; every call also carries x-pd-environment.
    def mint_access_token(self):
        """Mint a Pipedream developer access token via client_credentials. Returns
        ``{access_token, expires_in}`` (1h TTL). Minted fresh before each session/bind so
        MA is never handed an expired token; MA's auto-refresh (a refresh_token grant) is
        deliberately not relied upon."""
        url = f"{config.PIPEDREAM_API_BASE}/oauth/token"
        body = json.dumps({"grant_type": "client_credentials",
                           "client_id": self._client_id,
                           "client_secret": self._client_secret}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT) as resp:
            out = json.loads(resp.read() or b"{}")
        return {"access_token": out.get("access_token"),
                "expires_in": out.get("expires_in") or 3600}

    def _access_token(self):
        """A valid cached Pipedream dev token, refreshed a minute before expiry."""
        if not self._pd_token or time.time() >= self._pd_token_exp - 60:
            tok = _retry(self.mint_access_token)
            self._pd_token = tok["access_token"]
            self._pd_token_exp = time.time() + float(tok.get("expires_in") or 3600)
        return self._pd_token

    def _pd_path(self, path):
        """Project-scope a Connect path: '/accounts' -> '/connect/{project}/accounts'."""
        p = path.lstrip("/")
        return f"/connect/{self._project}/{p}" if not p.startswith("connect/") else f"/{p}"

    def _pd_request(self, method, path, body=None):
        url = f"{self._connect_base}{self._pd_path(path)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._access_token()}",
            "x-pd-environment": self._environment,
        })
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def create_connect_token(self, external_user_id, app):
        """Create a Connect token for this user and return ``{token, connect_url}`` — the
        UI opens ``connect_url`` (the hosted Connect page) in a browser tab. The token
        (``ctok_…``) is single-use, 4h."""
        out = _retry(lambda: self._pd_request("POST", "/tokens",
                                              {"external_user_id": external_user_id}))
        token = out.get("token") or out.get("connect_token")
        connect_url = (out.get("connect_link_url") or out.get("connect_url")
                       or (f"https://pipedream.com/_static/connect.html?"
                           f"token={urllib.parse.quote(token or '')}&connectLink=true"
                           f"&app={urllib.parse.quote(app)}"))
        return {"token": token, "connect_url": connect_url}

    def get_account(self, external_user_id, app):
        """Look up the account this user connected for ``app``, including its identity
        email (load-bearing for post-connect verification)."""
        q = urllib.parse.urlencode({"external_user_id": external_user_id, "app": app})
        out = _retry(lambda: self._pd_request("GET", f"/accounts?{q}"))
        accounts = out.get("accounts") or out.get("data") or []
        acc = accounts[0] if accounts else {}
        return {"connected": bool(acc),
                "account_id": acc.get("id") or acc.get("account_id"),
                "identity_email": (acc.get("identity") or {}).get("email")
                or acc.get("email")}

    def list_accounts(self, external_user_id):
        """All accounts connected for this customer (for display / isolation checks)."""
        q = urllib.parse.urlencode({"external_user_id": external_user_id})
        out = _retry(lambda: self._pd_request("GET", f"/accounts?{q}"))
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
        """Write the Pipedream connection into the vault as an mcp_oauth credential keyed
        to this customer's per-app MCP server URL. The bearer is Trace's project-scoped
        Pipedream dev token (minted fresh here); ``external_user_id`` and ``app`` ride in
        the URL, NOT in the credential. Idempotent: a credential already keyed to that URL
        is updated, not duplicated."""
        creds = self._vaults_ns().credentials
        tok = _retry(self.mint_access_token)
        expires_at = (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(time.time() + float(tok.get("expires_in") or 3600))))
        auth = {"type": "mcp_oauth",
                "mcp_server_url": config.pipedream_mcp_url(external_user_id, app),
                "access_token": tok["access_token"],
                "expires_at": expires_at}
        try:
            c = _retry(lambda: creds.create(vault_ref, auth=auth))
        except Exception as exc:                                # 409 duplicate -> update
            if "409" in str(exc) or "exist" in str(exc).lower():
                c = _retry(lambda: creds.update(vault_ref, auth=auth))
            else:
                raise
        return {"binding_ref": _idof(c) or f"{vault_ref}:{app}"}

    # --- Phase 5: READ-ONLY reads through the bound connection ---
    # There is no per-app "destination" endpoint. The primary dry-run read path is the
    # agent's own MCP read/list tools (discovered at runtime) on a COPY; the source is
    # never written. As a raw fallback Trace reads via the Connect Proxy, which injects
    # the user's credentials server-side. These helpers use that proxy.
    def read_destination(self, external_user_id, app, binding_ref, account_id=None):
        """READ-ONLY snapshot of the destination artifact via the Connect Proxy. Returns
        ``{kind, columns, rows, raw}``. NEVER writes."""
        q = urllib.parse.urlencode({"external_user_id": external_user_id,
                                    "app": app, **({"account_id": account_id} if account_id else {})})
        out = _retry(lambda: self._pd_request("GET", f"/proxy/destination?{q}"))
        return {"kind": out.get("kind"), "columns": out.get("columns") or [],
                "rows": out.get("rows") or [], "raw": out.get("raw")}

    def read_recent_trigger(self, external_user_id, app, binding_ref, limit=1, account_id=None):
        """READ-ONLY: the most recent trigger instances (e.g. recent inbound emails) via
        the Connect Proxy — a held-out test instance. Returns ``[{detail, raw}]``. NEVER
        writes."""
        q = urllib.parse.urlencode({"external_user_id": external_user_id, "app": app,
                                    "limit": limit,
                                    **({"account_id": account_id} if account_id else {})})
        out = _retry(lambda: self._pd_request("GET", f"/proxy/recent?{q}"))
        items = out.get("items") or out.get("data") or []
        return [{"detail": i.get("detail") or i.get("snippet") or "", "raw": i}
                for i in items[:limit]]
