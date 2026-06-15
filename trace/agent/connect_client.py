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
import ssl
import time
import urllib.parse
import urllib.request

from . import config
from .api_client import _retry, _idof, _data, _attr

BETA_VERSION = config.BETA_VERSION


def _ssl_context():
    """A TLS context that trusts certifi's CA bundle (the same roots the Anthropic SDK
    uses), so the stdlib Pipedream HTTPS calls don't depend on the OS cert store being
    wired up — the usual cause of ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer
    certificate`` on python.org/macOS installs. Falls back to the system store if certifi
    is somehow absent."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CONTEXT = _ssl_context()


def _with_app(url, app):
    """Return ``url`` guaranteed to carry an ``app={slug}`` query param. The Pipedream
    hosted connect page rejects a URL without it ("Please include the app in the Connect
    URL"), and Pipedream's returned ``connect_link_url`` omits it. Any existing ``app`` is
    left untouched; the token and other params are preserved."""
    parts = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    if not q.get("app"):
        q["app"] = app
    return urllib.parse.urlunparse(parts._replace(query=urllib.parse.urlencode(q)))


def _account_email(acc):
    """Best-effort extraction of the connected account's email from a Pipedream account
    object. There is no standard ``email`` field — the email may sit under a profile/identity
    block (returned only with credentials) or, for Google OAuth, in the account ``name``
    (Pipedream labels the connection with the email). Returns a string or None."""
    if not isinstance(acc, dict):
        return None
    # nested identity/profile/credentials blocks (present when credentials are included)
    for outer in ("identity", "profile", "credentials", "oauth"):
        block = acc.get(outer)
        if isinstance(block, dict):
            v = block.get("email") or block.get("email_address")
            if isinstance(v, str) and "@" in v:
                return v
    # flat candidates, then the account name (Google connections are named with the email)
    for key in ("email", "email_address", "external_id", "name"):
        v = acc.get(key)
        if isinstance(v, str) and "@" in v:
            return v
    return None


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
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT,
                                    context=_SSL_CONTEXT) as resp:
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
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT,
                                    context=_SSL_CONTEXT) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def create_connect_token(self, external_user_id, app):
        """Create a Connect token for this user and return ``{token, connect_url}`` — the
        UI opens ``connect_url`` (the hosted Connect page) in a browser tab. The token
        (``ctok_…``) is single-use, 4h. The hosted page REQUIRES the app slug in the URL, so
        it is always appended even when Pipedream returns a link without it."""
        out = _retry(lambda: self._pd_request("POST", "/tokens",
                                              {"external_user_id": external_user_id}))
        token = out.get("token") or out.get("connect_token")
        base = (out.get("connect_link_url") or out.get("connect_url")
                or (f"https://pipedream.com/_static/connect.html?"
                    f"token={urllib.parse.quote(token or '')}&connectLink=true"))
        return {"token": token, "connect_url": _with_app(base, app)}

    def get_account(self, external_user_id, app):
        """Look up the account this user connected for ``app``, including its identity email
        (load-bearing for post-connect verification). Pipedream's account object has no plain
        ``email`` field, so we request credentials (where a profile/email can appear) and also
        read the account ``name`` (Google connections are labelled with the email). The
        credentialed lookup is best-effort: if the project can't return credentials (managed
        OAuth), we fall back to the plain lookup."""
        base = {"external_user_id": external_user_id, "app": app}
        out = None
        try:
            q = urllib.parse.urlencode({**base, "include_credentials": "true"})
            out = _retry(lambda: self._pd_request("GET", f"/accounts?{q}"))
        except Exception:
            out = None
        if not (isinstance(out, dict) and (out.get("accounts") or out.get("data"))):
            q = urllib.parse.urlencode(base)
            out = _retry(lambda: self._pd_request("GET", f"/accounts?{q}"))
        accounts = out.get("accounts") or out.get("data") or []
        acc = accounts[0] if accounts else {}
        return {"connected": bool(acc),
                "account_id": acc.get("id") or acc.get("account_id"),
                "identity_email": _account_email(acc)}

    def list_apps(self):
        """List the apps available in the project (the slug-lookup endpoint). Used as a
        cheap preflight: a successful call exercises minting the dev token AND reaching the
        project. Returns ``[{slug, name}]``."""
        out = _retry(lambda: self._pd_request("GET", "/apps"))
        apps = out.get("apps") or out.get("data") or []
        return [{"slug": a.get("name_slug") or a.get("slug") or a.get("app"),
                 "name": a.get("name") or a.get("label")} for a in apps]

    def list_accounts(self, external_user_id):
        """All accounts connected for this customer (for display / isolation checks)."""
        q = urllib.parse.urlencode({"external_user_id": external_user_id})
        out = _retry(lambda: self._pd_request("GET", f"/accounts?{q}"))
        return [{"app": a.get("app"), "account_id": a.get("id") or a.get("account_id"),
                 "identity_email": (a.get("identity") or {}).get("email") or a.get("email"),
                 "connected": True}
                for a in (out.get("accounts") or out.get("data") or [])]

    def ensure_vault(self, external_user_id):
        """Find-or-create the customer's vault. The vault is keyed to the customer via
        ``metadata.external_user_id`` (the documented reuse mechanism); ``vaults.create``
        takes ``display_name`` + ``metadata`` (there is no ``name`` param)."""
        display_name = f"trace-connect-{external_user_id}"
        try:
            res = _retry(lambda: self._vaults_ns().list())
            for v in _data(res):
                meta = _attr(v, "metadata") or {}
                ext = meta.get("external_user_id") if isinstance(meta, dict) \
                    else _attr(meta, "external_user_id")
                if ext == external_user_id or _attr(v, "display_name") == display_name:
                    return {"vault_ref": _idof(v), "created": False}
        except Exception:
            pass
        v = _retry(lambda: self._vaults_ns().create(
            display_name=display_name, metadata={"external_user_id": external_user_id}))
        return {"vault_ref": _idof(v), "created": True}

    def _find_credential(self, vault_ref, mcp_server_url):
        """The id of an existing credential in ``vault_ref`` keyed to ``mcp_server_url``,
        or None. The server URL may sit at the top level or under ``auth`` on the returned
        credential, so check both."""
        try:
            res = _retry(lambda: self._vaults_ns().credentials.list(vault_ref))
        except Exception:
            return None
        for c in _data(res):
            url = _attr(c, "mcp_server_url")
            if url is None:
                auth = _attr(c, "auth") or {}
                url = auth.get("mcp_server_url") if isinstance(auth, dict) \
                    else _attr(auth, "mcp_server_url")
            if url == mcp_server_url:
                return _idof(c)
        return None

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
        mcp_server_url = config.pipedream_mcp_url(external_user_id, app)
        auth = {"type": "mcp_oauth", "mcp_server_url": mcp_server_url,
                "access_token": tok["access_token"], "expires_at": expires_at}
        display_name = f"Trace {app}"
        try:
            c = _retry(lambda: creds.create(vault_ref, display_name=display_name, auth=auth))
        except Exception as exc:                                # 409 duplicate -> update by id
            if "409" in str(exc) or "exist" in str(exc).lower():
                cred_id = self._find_credential(vault_ref, mcp_server_url)
                if not cred_id:
                    raise
                c = _retry(lambda: creds.update(cred_id, vault_id=vault_ref, auth=auth))
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
