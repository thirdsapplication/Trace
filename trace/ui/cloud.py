"""Phase 2 (desktop app) — the loopback→cloud HTTP proxy.

In desktop mode the local node (the same ``trace.ui.app`` server, bound to
127.0.0.1 inside the packaged app) forwards auth + pipeline requests to the
configured cloud backend, so the end-user UI talks to a single localhost origin
and its existing relative-URL + cookie design works unchanged. This module is
the one network primitive that crosses the boundary; it uses only the standard
library (consistent with the rest of the backend) plus ``certifi`` for a CA
bundle that doesn't depend on the OS trust store.

``http_proxy`` is deliberately content-type-agnostic (it relays HTML/CSS/JS for
the proxied UI assets as readily as JSON for the API) and never raises on an
HTTP error status — a 4xx/5xx from the cloud is relayed back to the UI verbatim,
which is exactly what a transparent proxy should do.
"""

import ssl
import urllib.error
import urllib.request

# Response headers we must NOT copy verbatim: they describe *our* hop and are set
# fresh by the relaying handler (or are connection-level and meaningless to copy).
_SKIP_RESPONSE_HEADERS = frozenset((
    "content-length", "transfer-encoding", "connection", "keep-alive",
    "content-encoding"))

_ssl_ctx = None


def _context():
    """A TLS context backed by certifi's CA bundle (built once). Falls back to a
    default context if certifi isn't importable — plain-HTTP (local two-node
    testing) never reaches this path."""
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import certifi
            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def http_proxy(base_url, method, path, headers=None, body=None, timeout=60):
    """Forward one request to ``base_url + path`` and return the response.

    Returns ``(status:int, headers:list[(name, value)], body:bytes)``. HTTP error
    statuses are returned, not raised. Hop-by-hop response headers are stripped so
    the caller sets its own Content-Length; ``Set-Cookie`` and ``Content-Type``
    are preserved so cloud auth cookies flow back to the browser.
    """
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        if v is not None:
            req.add_header(k, v)
    ctx = _context() if url.lower().startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            status, raw_headers, data = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:           # relay 4xx/5xx verbatim
        status, raw_headers, data = e.code, e.headers, e.read()
    out_headers = [(k, v) for k, v in raw_headers.items()
                   if k.lower() not in _SKIP_RESPONSE_HEADERS]
    return status, out_headers, data
