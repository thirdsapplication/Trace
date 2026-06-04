"""Capture-time redaction of obvious secrets / PII.

Privacy is a first-class requirement (handoff Section 5a): secrets and PII must be
redacted *before* anything is written to disk. This module is applied centrally in
``raw_writer`` so every capturer (local process and browser extension via the
collector) goes through the same filter.

Design notes:
- Conservative by intent. We redact things that are *obviously* secret/PII
  (credit-card numbers, SSNs, API keys/bearer tokens, private keys). We do NOT
  redact email addresses or names, because recipients are signal the
  reconstruction needs (ship-confirmation recipients, reply targets, etc.).
- Password fields are handled upstream, not here: the browser extension never
  captures input on ``input[type=password]`` and equivalents, and signals a
  password-focus state. Native-app password fields are a documented v1 gap
  (no OS-accessibility hook yet) — see README Section 9.
- Redaction is irreversible and happens at capture time, so the original secret
  never reaches ``raw_events.jsonl``.
"""

import re

REDACTION_TOKEN = "[REDACTED]"

# --- Patterns -------------------------------------------------------------

# 13-19 digit runs, optionally separated by spaces/dashes -> candidate card number.
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# US SSN-ish: 3-2-4 grouped digits.
_SSN = re.compile(r"\b\d{3}[ -]\d{2}[ -]\d{4}\b")

# Common secret token shapes: sk-..., AKIA..., ghp_..., long base64-ish blobs
# that follow a key-ish label, bearer tokens, "password=...", etc.
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),                  # OpenAI-style keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                       # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),             # GitHub tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),           # Slack tokens
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
               re.DOTALL),
]


def _luhn_ok(digits: str) -> bool:
    """Luhn check so we only redact things that actually look like card numbers,
    not arbitrary long numeric strings (e.g. order ids, tracking numbers)."""
    nums = [int(c) for c in digits if c.isdigit()]
    if not (13 <= len(nums) <= 19):
        return False
    checksum = 0
    parity = len(nums) % 2
    for i, n in enumerate(nums):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        checksum += n
    return checksum % 10 == 0


def _redact_cards(text: str) -> str:
    def repl(m):
        return REDACTION_TOKEN if _luhn_ok(m.group(0)) else m.group(0)
    return _CARD.sub(repl, text)


def redact_text(text):
    """Redact obvious secrets/PII from a single string. Returns the redacted string
    (or the input unchanged if it is not a non-empty string)."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(REDACTION_TOKEN, out)
    out = _SSN.sub(REDACTION_TOKEN, out)
    out = _redact_cards(out)
    return out


# Per-source map of payload fields whose values are free text and must be redacted.
_TEXT_FIELDS = {
    "keystroke": ["text"],
    "clipboard": ["content"],
    "window": ["title"],
}


def redact_payload(source, payload):
    """Return a redacted copy of a raw-event payload for the given source.

    Handles the flat text fields plus the nested structures (click targets and
    structured_read element values)."""
    if not isinstance(payload, dict):
        return payload
    p = dict(payload)

    for field in _TEXT_FIELDS.get(source, []):
        if field in p:
            p[field] = redact_text(p[field])

    # click target value
    if source == "click" and isinstance(p.get("target"), dict):
        tgt = dict(p["target"])
        if "value" in tgt:
            tgt["value"] = redact_text(tgt["value"])
        if "label" in tgt:
            tgt["label"] = redact_text(tgt["label"])
        p["target"] = tgt

    # bare target event
    if source == "target":
        for field in ("value", "label"):
            if field in p:
                p[field] = redact_text(p[field])

    # structured_read elements
    if source == "structured_read" and isinstance(p.get("elements"), list):
        new_elems = []
        for el in p["elements"]:
            if isinstance(el, dict):
                el = dict(el)
                if "value" in el:
                    el["value"] = redact_text(el["value"])
                if "label" in el:
                    el["label"] = redact_text(el["label"])
            new_elems.append(el)
        p["elements"] = new_elems

    return p
