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
        print("[embed] nothing to embed (all inputs blank)", flush=True)
        return None
    key = api_key or (os.environ.get("VOYAGE_API_KEY") or "").strip()
    if not key:
        print("[embed] VOYAGE_API_KEY is not set in THIS process -- lesson retrieval "
              "is inert here", flush=True)
        return None
    body = json.dumps({"model": VOYAGE_MODEL, "input": texts,
                       "input_type": input_type, "output_dimension": EMBED_DIM}).encode()
    req = urllib.request.Request(VOYAGE_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:                                  # noqa: BLE001
            detail = ""
        print(f"[embed] voyage refused ({e.code}) for input_type={input_type!r}: "
              f"{detail}", flush=True)
        return None
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as e:
        print(f"[embed] voyage unreachable ({type(e).__name__}: {e}) for "
              f"input_type={input_type!r}", flush=True)
        return None
    rows = data.get("data") or []
    out = [row.get("embedding") for row in rows]
    if len(out) != len(texts) or any(v is None for v in out):
        # Never log the text itself -- lessons are user content. Counts only.
        print(f"[embed] voyage returned {len(out)} vector(s) for {len(texts)} input(s), "
              f"input_type={input_type!r} -- treating as unusable", flush=True)
        return None
    return out
