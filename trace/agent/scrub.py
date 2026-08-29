"""Privacy pre-call — de-identify one agent's batch BEFORE any other agent can read it.

Collective learning only works if an agent can read what other agents actually did, and
what agents actually do is full of their owners' customers: names, addresses, invoice
amounts, order ids. This engine runs on the PRODUCING agent's own learning cycle, so the
raw material never leaves the cycle that owns it — what leaves is a rewrite in which every
customer-identifying detail has become a generic description ("Mario's Pizzeria" ->
"restaurant client A") and the mechanics a peer needs (tool names, errors, retries,
ordering) are untouched.

Trust is placed in the checks, not in the model. A scrub is accepted only after a
deterministic structural check (a model that summarised instead of substituting is the
likeliest failure, and it is free to detect), a deterministic leak sweep that ignores what
the model claimed and simply hunts for identifier SHAPES, and finally a second, independent
model that judges the scrubbed text on its own. Cheap checks run first; the paid one last.

Everything here is best-effort and fails closed: the return value on any doubt is None, and
None costs the fleet one publish, never an agent its cycle.

Failing closed is not the same as failing often, and the first version of this module
confused the two. The sweep rejected any run of four or more digits, on the theory that a
rejected batch is a non-event while a leaked one is a customer's data in another
customer's payload. But a year is four digits. So is a token count, a spreadsheet range,
a row id. It rejected seven batches out of seven in production, published nothing, and
left cross-agent learning looking like a tuning problem for a day. A gate that fires on
everything does not protect anything — it removes the feature quietly, in the one place
nobody is looking. The sweep now catches shapes that are hard to produce by accident and
leaves the fuzzy cases to the two checks built for them.
"""

import json
import re

from . import config, model_call
from .configgen import _parse_json_object
from .scrub_prompt import SCRUB_SYSTEM, VERIFY_SYSTEM

DEFAULT_MODEL = config.SCRUB_MODEL
VERIFY_MODEL = config.SCRUB_VERIFY_MODEL

# The only documents that may be scrubbed and travel. /shared_lessons.md is deliberately
# NOT here: it is Trace-owned and is already distilled from OTHER agents' material, so
# publishing it as peer material would let a lesson re-enter the fleet as fresh peer
# evidence and be counted a second time — one agent's opinion coming back as independent
# corroboration of itself.
#
# /lessons_learned.md is a WEAKER version of that same objection, accepted knowingly.
# Once collective learning is on it is itself peer-influenced, so publishing it does let
# a technique propagate agent to agent and, over enough cycles, return to where it
# started looking like corroboration. That is not a leak and it is largely the point —
# a good technique spreading is the feature — but it means a BAD one can amplify rather
# than die out, which the per-agent token measurement in prior_cycles cannot currently
# attribute back to a peer. Excluding it instead would gut the feature: a peer's curated
# lessons are the most useful thing in a bundle, far more so than its raw transcripts.
# If amplification ever shows up in practice, the fix is to mark peer-derived lessons in
# the file so the next cycle can drop them first — not to stop publishing the document.
# An EXCLUSION, not a whitelist, since the learning engine may now create documents of
# its own: a store is whatever is in it, and a /playbook.md this agent worked out is
# exactly the kind of thing a similar agent should see.
UNSHAREABLE_DOCS = (config.MEMORY_SHARED_DOC,)


def _default_model_fn(model=DEFAULT_MODEL):
    import anthropic
    # Bounded: a bare client defaults to ~10 minutes per attempt WITH retries, so one
    # stuck scrub could pin a worker thread for the better part of an hour.
    client = anthropic.Anthropic(timeout=config.SCRUB_REQUEST_TIMEOUT,
                                 max_retries=config.SCRUB_RETRIES)

    def call(system, user):
        # Streaming, not create_text: the reply restates every session AND every document
        # in full, and above ~16K max_tokens a non-streaming request does not fail — it
        # hangs until the HTTP timeout, with no error to read (see model_call's docstring).
        return model_call.create_text_streaming(client, model, system, user,
                                                max_tokens=config.SCRUB_MAX_TOKENS)

    return call


def _default_verify_model_fn(model=VERIFY_MODEL):
    import anthropic
    client = anthropic.Anthropic(timeout=config.SCRUB_REQUEST_TIMEOUT,
                                 max_retries=config.SCRUB_RETRIES)

    def call(system, user):
        # The ordinary non-streaming call. max_tokens is NOT tiny, and the first version's
        # 100 was a bug that took a production round to find: Opus 5 runs adaptive
        # thinking by DEFAULT, thinking tokens count against max_tokens, and a ceiling
        # that low is spent before any text block is emitted. The gate then returned an
        # empty string, which fails closed — so every batch was refused with "did not
        # answer safe" and it read exactly like the model judging the content unsafe.
        # The answer is still one boolean; the headroom is for the reasoning in front of
        # it. temperature=None: Opus 5 rejects sampling parameters with a 400.
        return model_call.create_text(client, model, system, user,
                                      max_tokens=config.SCRUB_VERIFY_MAX_TOKENS,
                                      temperature=None)

    return call


def _norm_path(path):
    """Doc paths are compared against a whitelist, so a missing leading slash must not
    silently turn an allowed doc into a rejected one (or vice versa)."""
    p = str(path or "")
    return p if p.startswith("/") else "/" + p


def _clip_transcript(text):
    return str(text or "")[:config.SCRUB_SESSION_CLIP]


def _sanitize_memory(docs):
    """Every document except UNSHAREABLE_DOCS, with non-empty string content, clipped.
    Applied on the way IN and again on the way OUT: the model echoes back what it was
    handed, but its answer is what gets published, so the rule is enforced in code both
    times — including against a path the model invented, which would otherwise ride out
    of here having never been swept."""
    out = {}
    if not isinstance(docs, dict):
        return out
    for path, text in docs.items():
        p = _norm_path(path)
        if not p or p in UNSHAREABLE_DOCS:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        out[p] = text[:config.SCRUB_DOC_CLIP]
    return out


def build_payload(session_records, memory_docs):
    """The dict json.dumps'd into the scrub call's user message: the batch as the learning
    call would see it, clipped so a VERBATIM rewrite of it still fits inside
    SCRUB_MAX_TOKENS. Unreadable docs (None) are dropped rather than sent as
    "(unavailable)" the way learn.build_payload does — there is nothing to scrub in a
    document nobody could read, and nothing to publish either."""
    sessions = [{"session_id": rec.get("session_id"),
                 "ran_at": rec.get("ran_at"),
                 "status": rec.get("status"),
                 "tokens": rec.get("tokens"),
                 "transcript": _clip_transcript(rec.get("transcript"))}
                for rec in (session_records or [])]
    return {"sessions": sessions, "memory": _sanitize_memory(memory_docs)}


def scrub_batch(session_records, memory_docs, model_fn):
    """ONE scrub call over the batch. Returns the parsed answer — ``{"sessions", "memory",
    "redactions"}`` — or None when the call or the parse failed. Never raises: this runs
    inside a learning cycle, and a failed scrub must cost the fleet one publish, not the
    agent its cycle."""
    if not session_records:
        return None
    user = json.dumps(build_payload(session_records, memory_docs),
                      ensure_ascii=False, default=str)
    try:
        reply = model_fn(SCRUB_SYSTEM, user)
    except Exception as e:                                 # noqa: BLE001 — best-effort, but
        # say WHY: a reason-less "scrub failed" line cost a debugging round in learn.py.
        print(f"[scrub] scrub call failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        return None
    obj = _parse_json_object(reply)
    if obj:
        return obj
    # An unparseable reply used to surface as "produced nothing", which is indistinguishable
    # from a call that never happened — and in production that ambiguity hid four failures
    # in a row. A verbatim rewrite is the largest reply anything here asks for, so the two
    # things worth knowing are how big it got and whether it stops mid-structure.
    text = str(reply or "")
    cut = (", ends mid-structure — raise SCRUB_MAX_TOKENS or lower the clips"
           if text.strip() and not text.rstrip().endswith(("}", "```")) else "")
    print(f"[scrub] reply did not parse as JSON: {len(user)} chars in, "
          f"{len(text)} chars out{cut}. Tail: {text.rstrip()[-160:]!r}", flush=True)
    return None


def structural_check(session_records, scrubbed):
    """(ok, reason) — did the model SUBSTITUTE, or did it summarise? Same session ids, same
    count, and the same number of lines in every transcript.

    Summarising is the likeliest way a scrub goes wrong, and it is silent: the answer still
    parses, still reads plausibly, and has quietly thrown away the retry patterns and
    orderings that are the whole reason this material travels. Line counts catch it for
    free, before either model is paid again.
    """
    originals = {}
    for rec in (session_records or []):
        sid = str(rec.get("session_id") or "")
        if sid:
            originals[sid] = _clip_transcript(rec.get("transcript"))
    got = (scrubbed or {}).get("sessions")
    if not isinstance(got, list):
        return False, "no sessions in the scrubbed payload"
    if len(got) != len(originals):
        return False, f"session count changed: {len(originals)} in, {len(got)} out"
    seen = set()
    for entry in got:
        if not isinstance(entry, dict):
            return False, "session entry is not an object"
        sid = str(entry.get("session_id") or "")
        if sid not in originals:
            return False, f"unknown session id {sid!r}"
        if sid in seen:
            return False, f"duplicate session id {sid!r}"
        seen.add(sid)
        # Compare against the CLIPPED original: the model only ever saw the clipped copy,
        # so counting lines in the full record would fail every over-long session.
        want = len(originals[sid].splitlines())
        have = len(str(entry.get("transcript") or "").splitlines())
        if have != want:
            return False, (f"session {sid} transcript {want} lines in, {have} out "
                           f"(summarised?)")
    return True, ""


# Identifier SHAPES. This sweep is unconditional — it does not care what the model
# declared in redactions, because the dangerous case is the identifier it never declared
# at all. A hit fails the whole batch even when the redactions list is empty.
#
# PRECISION IS THE WHOLE PROBLEM HERE. The first version of this sweep rejected a run of
# four or more digits outright, on the reasoning that order numbers and account numbers
# look like that. So does a year. So does a token count, a spreadsheet range, a row id,
# an HTTP status followed by a length. In production it rejected every batch it was ever
# shown — seven for seven — and because a rejection publishes nothing, the peer table
# stayed empty and cross-agent learning looked like a similarity-threshold problem for a
# day. A gate that fires on everything protects nothing; it just turns the feature off
# where no one can see it.
#
# So this sweep now catches only shapes that are hard to produce by accident: an address
# with an @, a Luhn-valid card number, a conventionally formatted phone number, a URL
# carrying query parameters. Everything fuzzier — an order id, an invoice number, an
# account reference — is the scrubber's job to replace (the prompt names them) and the
# declared-literal check's job to verify, with the independent model pass behind both.
# The deterministic sweep is a backstop for the obvious, not the primary defence.
_DATELIKE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)?"   # ISO date/time
    r"|\b\d{2}:\d{2}(?::\d{2})?\b"                                       # bare clock time
    r"|\b\d{10,13}\b"                                                    # epoch s / ms
)


def _luhn(digits):
    """True if a digit string passes the Luhn checksum — the standard test every payment
    card satisfies and a run of arbitrary digits passes only about a tenth of the time.
    Without it, any 16-digit identifier reads as a card."""
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


_CARD_CANDIDATE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")


def _card_like(text):
    """A payment-card number: 13-19 digits, Luhn-valid, and either grouped by separators
    or of a length a card actually uses. A 13-digit millisecond timestamp is bare and the
    wrong length, so it does not reach Luhn at all."""
    for m in _CARD_CANDIDATE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if not 13 <= len(digits) <= 19 or not _luhn(digits):
            continue
        grouped = bool(re.search(r"\d[ -]\d", raw))
        if grouped or len(digits) in (15, 16):
            return True
    return bool(_IBAN.search(text))


# +1 (555) 123-4567 and friends. A separator is REQUIRED — a bare digit run is an
# identifier of some kind, not evidence of a phone number — and dates and clock times are
# stripped before this runs, because "2026-08-28" is otherwise a nine-digit "phone".
_PHONE = re.compile(r"(?<![\d.])\+?\d[\d ().-]{5,13}\d(?![\d.])")


def _phone_like(text):
    for m in _PHONE.finditer(_DATELIKE.sub(" ", text)):
        raw = m.group(0)
        if not re.search(r"[ ().-]", raw):        # separatorless: just a number
            continue
        if 7 <= len(re.sub(r"\D", "", raw)) <= 15:
            return True
    return False


_SWEEP = (
    # someone@somewhere.tld — the most direct re-identifier there is, and not a shape
    # anything else in a transcript takes.
    ("email address", lambda t: bool(re.search(
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", t))),
    ("card/IBAN-shaped string", _card_like),
    ("phone-shaped number", _phone_like),
    # A URL with a query string: ?id=, ?email=, share tokens — the parameters carry the
    # identity even when the host does not.
    ("url with a query string", lambda t: bool(re.search(r"https?://\S+\?\S", t))),
)


def _sweep_text(scrubbed):
    """The free text of a scrubbed payload — every transcript and every document — as one
    string.

    Deliberately NOT the whole serialised object: session_id, ran_at and tokens are
    structural fields the scrub is REQUIRED to copy through untouched, and an ISO
    timestamp or a five-figure token total trips the four-digit rule on literally every
    payload that will ever be produced. They carry no identity. The text does.
    """
    parts = []
    for entry in ((scrubbed or {}).get("sessions") or []):
        if isinstance(entry, dict):
            parts.append(str(entry.get("transcript") or ""))
    memory = (scrubbed or {}).get("memory")
    if isinstance(memory, dict):
        parts.extend(str(text or "") for text in memory.values())
    return "\n".join(parts)


def _strings(value):
    """Every string anywhere in a nested structure, as a flat list. Keys as well as
    values: a model that answered {"Mario's Pizzeria": "..."} put the literal in a key."""
    out = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            out.extend(_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_strings(v))
    return out


def leak_sweep(scrubbed, redactions):
    """(ok, reason) — did anything identifying survive? Three checks over the scrubbed
    payload: every declared literal is gone, every declared pseudonym is actually there
    (a declaration the model never carried out), and no identifier shape appears in the
    free text at all. Reasons never quote what was found: this line gets printed."""
    # Everything the model returned EXCEPT its own redactions list: that list quotes every
    # original literal by design, so searching it for those literals would fail every
    # scrub ever written. Any other key the model invented is still searched.
    content = {k: v for k, v in (scrubbed or {}).items() if k != "redactions"}
    # Walked, not json.dumps'd. Serialising first would compare against ESCAPED text, so a
    # literal holding a quote, a backslash, a newline or a tab — O'Brien's Bar, a Windows
    # path, a pasted signature block — could survive verbatim in the published string and
    # still never match here. That would silently disarm the one check in this file that
    # does not take the model's word for anything.
    low = "\n".join(_strings(content)).lower()
    for item in (redactions or []):
        if not isinstance(item, dict):
            return False, "redaction entry is not an object"
        literal = str(item.get("literal") or "")
        pseudonym = str(item.get("pseudonym") or "")
        if not literal.strip():
            continue                     # an empty literal declares nothing to look for
        if not pseudonym.strip():
            return False, "redaction declares a literal but no pseudonym"
        # Case-insensitive: "MARIO'S PIZZERIA" in a subject line is the same leak.
        if literal.lower() in low:
            return False, f"declared literal still present (pseudonym {pseudonym!r})"
        if pseudonym.lower() not in low:
            return False, f"pseudonym {pseudonym!r} declared but never used"
    text = _sweep_text(scrubbed)
    for label, detect in _SWEEP:
        if detect(text):
            return False, f"{label} survived in the scrubbed text"
    return True, ""


def verify_scrub(scrubbed, model_fn):
    """True iff an independent model, seeing ONLY the scrubbed payload, says no real
    person, company, or account can be identified from it. It exists for the leak the
    regexes cannot see: a description that names nobody and still points at one real
    business.

    FAILS CLOSED — empty input, an exception, an unparseable answer, or anything other than
    a literal true returns False (same posture as generalize.verify_lesson_scrub), because
    a wrong True puts one customer's material in another customer's payload.
    """
    if not isinstance(scrubbed, dict):
        return False
    payload = {"sessions": scrubbed.get("sessions") or [],
               "memory": scrubbed.get("memory") or {}}
    if not payload["sessions"] and not payload["memory"]:
        return False
    # The gate never sees "redactions": that list holds the ORIGINAL literals, so sending
    # it would hand the verifier the very identities it is being asked to hunt for.
    user = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        reply = model_fn(VERIFY_SYSTEM, user)
    except Exception as e:                                 # noqa: BLE001 — fail closed
        print(f"[scrub] verify call failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        return False
    obj = _parse_json_object(reply) or {}
    if obj.get("safe") is True:
        return True
    # "did not answer safe" covers two very different things — the gate judged the text
    # unsafe, or the gate never answered at all — and conflating them cost a production
    # round when an empty reply (max_tokens spent on thinking) read as a refusal.
    text = str(reply or "")
    if not text.strip():
        print("[scrub] the privacy gate returned NOTHING — this is a broken call, not a "
              "judgement (check SCRUB_VERIFY_MAX_TOKENS against the model's thinking)",
              flush=True)
    elif not obj:
        print(f"[scrub] the privacy gate did not answer JSON. Reply: {text[:200]!r}",
              flush=True)
    else:
        print(f"[scrub] the privacy gate judged the batch unsafe: {obj!r}", flush=True)
    return False


def _publishable_sessions(session_records, scrubbed):
    """The sessions actually published: scrubbed TEXT, with the metadata taken from the
    records we already hold rather than from the answer. Two reasons for the projection —
    a field the model invented would ride out of here having never been swept (nothing
    downstream knows to look at a key that is not in the contract), and a status or a
    token count the model "tidied" would misreport what the batch really cost.

    session_id and ran_at are dropped here, and this is the last point at which they
    could be. Both are required INPUTS — structural_check matches sessions by id, and
    without that a model could quietly drop or rename one — but neither may be published:
    a platform session id identifies the producing customer's run to anyone who can see
    the platform, and a run timestamp discloses when that customer's business operates.
    Neither carries any signal a reader needs, because order is the list's order. What
    replaces the id is an ordinal, so a reader can still say "in session 2 it retried".
    """
    by_id = {str(rec.get("session_id") or ""): rec for rec in (session_records or [])}
    out = []
    for entry in ((scrubbed or {}).get("sessions") or []):
        if not isinstance(entry, dict):
            continue
        rec = by_id.get(str(entry.get("session_id") or "")) or {}
        out.append({"index": len(out) + 1,
                    "status": rec.get("status"),
                    "tokens": rec.get("tokens"),
                    "transcript": str(entry.get("transcript") or "")})
    return out


def _reject(reason):
    print(f"[scrub] rejected — {reason}", flush=True)
    return None


def scrub_and_verify(session_records, memory_docs, model_fn, verify_model_fn):
    """The whole gate: scrub, then structural check, then leak sweep, then the independent
    privacy model — deterministic checks first so a summarised or leaky answer is thrown
    out before a second model is paid to read it.

    With ``config.SCRUB_CHECKS_ENABLED`` false (TRACE_SCRUB_CHECKS=0) all three are
    skipped and whatever the scrub model returned is published. That is a debugging
    switch for telling "nothing qualified" apart from "this is broken", which the logs
    otherwise render identically — not a mode anything should run in. The scrub itself
    still happens; nothing verifies it did anything.

    Returns ``{"sessions": [...], "memory": {path: text}}`` ready to publish, or None.
    Never raises; prints one reason line on rejection.
    """
    scrubbed = scrub_batch(session_records, memory_docs, model_fn)
    if not scrubbed:
        return _reject("scrub call produced nothing")
    if not config.SCRUB_CHECKS_ENABLED:
        # Loud on EVERY publish, not once at startup: a flag set for one debugging run and
        # forgotten is exactly how unverified material ends up shared, and the log is the
        # only place that would show it.
        print("[scrub] *** CHECKS DISABLED (TRACE_SCRUB_CHECKS=0) *** publishing this "
              "batch with NOTHING verifying the scrub removed anything. Delete these "
              "bundles before any real customer data goes near this path.", flush=True)
    else:
        ok, reason = structural_check(session_records, scrubbed)
        if not ok:
            return _reject(reason)
        ok, reason = leak_sweep(scrubbed, scrubbed.get("redactions") or [])
        if not ok:
            return _reject(reason)
        if not verify_scrub(scrubbed, verify_model_fn):
            return _reject("privacy gate did not answer safe")
    # "redactions" is a literal -> pseudonym map: the one artifact that can UNDO this
    # scrub. It is used by the checks above and dropped here — it must never be persisted,
    # logged, or passed onward to any caller.
    return {"sessions": _publishable_sessions(session_records, scrubbed),
            "memory": _sanitize_memory(scrubbed.get("memory"))}
