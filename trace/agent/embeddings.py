"""Voyage embeddings for decision-memory RAG (Gap #3). One function behind a wall so the
rest of Trace never touches the HTTP layer or the provider name. Degrades to ``None`` when
no ``VOYAGE_API_KEY`` is set, so dev / file-mode runs (and tests) work without embeddings.

Provider: Voyage ``voyage-3.5`` at 1024 output dims (matches decision_embeddings after
migration 006). Swap this one module to change providers.
"""

import json
import os
import urllib.error
import urllib.request

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-3.5"
EMBED_DIM = 1024
_TIMEOUT = 30


def is_configured():
    return bool((os.environ.get("VOYAGE_API_KEY") or "").strip())


def embed(texts, input_type="document", api_key=None):
    """Embed ``texts`` -> a list of ``EMBED_DIM``-float vectors, or ``None`` when embeddings
    are unavailable (no key, empty input, or an API/parse error). Never raises.

    ``input_type`` is ``"document"`` when storing a lesson and ``"query"`` when searching —
    Voyage embeds the two asymmetrically for better retrieval. A single string is accepted
    and returns a one-element list."""
    if isinstance(texts, str):
        texts = [texts]
    texts = [str(t) for t in (texts or []) if str(t).strip()]
    if not texts:
        return None
    key = api_key or (os.environ.get("VOYAGE_API_KEY") or "").strip()
    if not key:
        return None
    body = json.dumps({"model": VOYAGE_MODEL, "input": texts,
                       "input_type": input_type, "output_dimension": EMBED_DIM}).encode()
    req = urllib.request.Request(VOYAGE_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None
    rows = data.get("data") or []
    out = [row.get("embedding") for row in rows]
    if len(out) != len(texts) or any(v is None for v in out):
        return None
    return out
