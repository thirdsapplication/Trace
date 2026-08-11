"""Deploy-time MCP tool preflight (Phase 6 guard).

The tool surface a live agent gets is discovered at runtime from Pipedream's hosted
MCP server — Trace never names individual tools. That means a server serving a
reduced toolset is invisible until a live run needs the missing tool (the Aug 2026
incident: the old ``/v3`` surface served a Google Sheets toolset with no update tool,
and the agent could only halt mid-workflow). This module lists each per-app server's
tools BEFORE the agent is created, so the deploy record shows exactly what the agent
will have and flags obvious capability gaps while a human is still looking.

Stdlib-only (urllib + json), same as the rest of the connection layer; TLS and error
surfacing reuse ``connect_client._urlopen``. Everything here is read-only against
Pipedream (an MCP ``initialize`` + ``tools/list`` handshake — no tool is executed)
and best-effort at the call site: a preflight failure must never block a deploy.
"""

import json
import urllib.error
import urllib.request

from . import config
from .connect_client import _SSL_CONTEXT

# MCP protocol revision sent in the initialize handshake. A server that prefers
# another revision still answers tools/list; this is a label, not a pin.
MCP_PROTOCOL_VERSION = "2025-06-18"

# Capability patterns that a workflow-critical toolset should cover. Today one rule:
# a sheets-like app whose toolset can add rows but can neither update nor upsert them
# will strand any workflow that edits existing records (the exact Aug 2026 failure).
_UPDATE_MARKERS = ("update", "upsert")


def _mcp_post(url, token, body, session_id=None):
    """One JSON-RPC POST to an MCP server. Returns ``(parsed_or_raw, session_id)`` —
    the server's ``mcp-session-id`` response header is captured on the way out so the
    handshake can carry it forward. The body may be plain JSON or an SSE stream
    (``data: {...}`` lines); both are handled by the caller via ``_jsonrpc_results``."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=headers)
    # Same semantics as connect_client._urlopen (certifi TLS, HTTP error BODY
    # surfaced in the raised error) — inlined because the mcp-session-id response
    # header is only reachable from the live response object, which _urlopen discards.
    try:
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT,
                                    context=_SSL_CONTEXT) as resp:
            raw, sid = resp.read(), resp.headers.get("mcp-session-id")
    except urllib.error.HTTPError as e:
        try:
            detail = (e.read() or b"").decode("utf-8", "replace").strip()[:2000]
        except Exception:
            detail = "<unreadable response body>"
        err = RuntimeError(f"Pipedream MCP {e.code} {e.reason} on POST {url}: "
                           f"{detail or '<empty body>'}")
        err.status = e.code
        raise err from e
    return raw.decode("utf-8", "replace"), (sid or session_id)


def _jsonrpc_results(raw):
    """Every JSON-RPC ``result`` object in a response body that may be plain JSON or
    an SSE stream (``data: {...}`` lines)."""
    out = []
    for line in raw.splitlines() or [raw]:
        line = line[5:].strip() if line.startswith("data:") else line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and isinstance(msg.get("result"), dict):
            out.append(msg["result"])
    return out


def list_mcp_tools(token, url):
    """The tool names served by one MCP server URL, via the standard handshake:
    ``initialize`` (capturing any ``mcp-session-id``) -> ``notifications/initialized``
    (best-effort) -> ``tools/list``. Returns a sorted list of names; raises on
    transport/auth errors (callers decide whether that blocks anything)."""
    init_raw, sid = _mcp_post(url, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "trace-preflight", "version": "1"}}})
    try:
        _mcp_post(url, token, {"jsonrpc": "2.0",
                               "method": "notifications/initialized"}, sid)
    except Exception:
        pass                                     # some servers 202/reject the notify
    raw, _ = _mcp_post(url, token, {"jsonrpc": "2.0", "id": 2,
                                    "method": "tools/list", "params": {}}, sid)
    names = set()
    for result in _jsonrpc_results(raw):
        for tool in result.get("tools") or []:
            name = tool.get("name")
            if name:
                names.add(name)
    return sorted(names)


def toolset_warnings(toolsets):
    """Human-readable capability warnings for ``{app_slug: [tool names]}``. Rule:
    an app whose toolset includes an add/append row tool but NO update/upsert tool
    cannot edit existing records — the failure mode that strands workflows silently."""
    warnings = []
    for slug, names in (toolsets or {}).items():
        lower = [n.lower() for n in names]
        adds_rows = any("add" in n and "row" in n for n in lower)
        can_update = any(m in n for n in lower for m in _UPDATE_MARKERS)
        if adds_rows and not can_update:
            warnings.append(
                f"{slug}: served toolset has append tools but NO update/upsert tool "
                f"({len(names)} tools) — workflows that edit existing records will "
                f"stall at run time")
    return warnings


def preflight_apps(client, external_user_id, app_slugs):
    """List the served tools for every per-app MCP server this customer's agent will
    get. Returns ``{"toolsets": {slug: [names]}, "errors": {slug: str},
    "warnings": [str]}``. One dev token is minted (via the injected ConnectClient)
    and reused; per-app failures are recorded, never raised."""
    toolsets, errors = {}, {}
    try:
        token = client.mint_access_token()["access_token"]
    except Exception as exc:
        return {"toolsets": {}, "warnings": [],
                "errors": {"*": f"could not mint Pipedream token: {exc}"}}
    for slug in app_slugs or []:
        try:
            toolsets[slug] = list_mcp_tools(
                token, config.pipedream_mcp_url(external_user_id, slug))
        except Exception as exc:
            errors[slug] = str(exc)
    return {"toolsets": toolsets, "errors": errors,
            "warnings": toolset_warnings(toolsets)}
