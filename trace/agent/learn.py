"""Learning engine — periodic Fable-driven self-improvement (learning cycles).

Every ``config.LEARNING_CYCLE_SESSIONS`` completed sessions, ONE ``model_fn(system,
user) -> str`` call reviews the batch — each session's compacted transcript, outcome
status, and token cost — together with the agent's memory documents, the outcomes of
prior cycles, and (when a similar agent was found) privacy-scrubbed material from
that peer, then returns a full replacement ``lessons_learned.md`` aimed at making
future sessions more accurate and more token-efficient. Deterministic parsing;
best-effort — any failure returns None so a bad call never blocks a run.

Peers are here because an agent reading only its own history can do no better than
relearn its own waste, one customer at a time: the re-read another agent already
worked out it could drop is a saving this one should not have to pay for twice. What
peers do NOT do is widen anything. The material arrives already scrubbed and
de-identified, it is read for technique rather than for fact (the prompt's hard
rules), and it can reach no document the agent's own sessions could not, whoever the
lesson came from.

The engine owns the WHOLE store. It may rewrite any document, create documents that
did not exist, and delete any document — the memory an agent runs on is its to design,
not a fixed set of three files. That is a deliberate widening of an earlier contract in
which only ``lessons_learned.md`` could be touched, and it carries a real hazard the
code no longer guards: ``current_workflow_status.md`` holds the cumulative "Already
handled" ledger that MEMORY_UPDATE_DIRECTIVE tells the agent to skip work against. A
cycle that drops or paraphrases those entries makes the agent redo finished work
against live systems — re-sent mail, re-entered orders. Nothing here prevents that;
the system prompt carries the warning instead, by explicit product decision.

What ``_sanitize_docs`` still enforces is that a path is a path: inside the store, no
traversal, no absurd length, non-empty content, clipped to the size contract. The
transcripts this engine reads carry external content written by outside parties (a
prompt-injection surface) and whatever it writes steers every future session, so the
shape of what it may write is checked in code even though the SET no longer is.
"""

import json
import re

from . import activity, config, model_call
from .configgen import _parse_json_object
from .learn_prompt import SYSTEM_PROMPT

DEFAULT_MODEL = config.LEARNING_MODEL

# A memory store is flat: documents live at "/name.md" under the mounted directory.
# These bound what a PATH may be, not which paths exist — the engine may create any
# document it wants, but not one whose name would escape the store, collide with a
# directory node, or be unreadable to the agent that has to open it.
MAX_DOC_PATH = 128
STORED_OUTCOMES = ("kept", "reverted", "banned")
_PATH_OK = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._-]*\.md$")


def _default_model_fn(model):
    import anthropic
    # Bounded: a bare client defaults to ~10 minutes per attempt WITH retries, so one
    # stuck analysis could pin a worker thread (and its share of the account's API
    # headroom) for the better part of an hour.
    client = anthropic.Anthropic(timeout=config.LEARNING_REQUEST_TIMEOUT,
                                 max_retries=config.LEARNING_RETRIES)

    def call(system, user):
        # temperature=None: Fable 5 removed sampling parameters and 400s any
        # request that carries one, so the learning call must never send it.
        return model_call.create_text(client, model, system, user,
                                      max_tokens=config.LEARNING_MAX_TOKENS,
                                      temperature=None)

    return call


def session_record(run, events):
    """One session's entry in the learning payload: what it did (compacted
    transcript), how it ended, and what it cost."""
    return {
        "session_id": run.get("run_session_id"),
        "ran_at": run.get("ran_at") or run.get("started_at"),
        "status": activity.session_status(events),
        "tokens": activity.session_token_usage(events),
        "transcript": activity._compact(events)[:config.LEARNING_TRANSCRIPT_CLIP],
    }


def log_lists(changes):
    """The three standing lists, derived in CODE from the recorded numbers.

    Not stored as flags: a change that missed the bar by a point on a noisy batch and one
    that made performance fifteen percent worse are different evidence, and a pass/fail
    column would have destroyed the difference the moment it was written. Deriving the
    lists from ``baseline`` and ``observed`` keeps the distinction available forever.

    - kept: in the store and working. Techniques worth extending.
    - retryable: reverted, but performance was still BETTER than baseline — it missed the
      bar rather than failing. May be proposed once more with a stated reason.
    - banned: made performance worse, or was reverted twice. Permanent.
    """
    kept, retryable, banned = [], [], []
    reverts = {}
    # The stored vocabulary, which is also agent_changes_outcome_check's. decide() answers
    # in verbs and app._settle_pending maps them; anything else here is a bug upstream.
    for c in (changes or []):
        cid = c.get("change_id")
        root_id = c.get("retry_of") or cid
        if c.get("outcome") in ("reverted", "banned"):
            reverts[root_id] = reverts.get(root_id, 0) + 1
    for c in (changes or []):
        outcome = c.get("outcome")
        if outcome not in STORED_OUTCOMES:
            # Not just "if not outcome". An unsettled change has no outcome and belongs on
            # no list; but a value from the WRONG vocabulary — "revert" where "reverted"
            # was meant — used to fall through to retryable, quietly filing a failure as a
            # near miss worth trying again. Unknown is ignored, loudly enough that a
            # mismatch shows up as empty lists rather than as wrong ones.
            continue
        entry = {"change_id": c.get("change_id"), "summary": c.get("summary"),
                 "technique": c.get("technique"), "baseline": c.get("baseline"),
                 "observed": c.get("observed"), "reason": c.get("reason")}
        if outcome == "kept":
            kept.append(entry)
            continue
        root_id = c.get("retry_of") or c.get("change_id")
        worse = (c.get("observed") is not None and c.get("baseline") is not None
                 and c["observed"] >= c["baseline"])
        if outcome == "banned" or worse or reverts.get(root_id, 0) >= 2:
            banned.append(entry)
        else:
            retryable.append(entry)
    return {"kept": kept, "retryable": retryable, "banned": banned}


def decide(pending, observed, baseline, valid_rate, base_valid_rate):
    """The verdict, computed in CODE. Returns ``(decision, threshold, reason)``.

    The spec had the analyst apply the bar and report the arithmetic. It does still state
    a verdict — that is discipline, and a disagreement between its answer and this one is
    worth logging — but what EXECUTES is this function. A model can slip on arithmetic, and
    the one place a graded party should never hold the pen is the grade.

    The valid rate is checked FIRST and can revert a change whose score passed. Excluding
    damaged sessions hides damage rather than catching it: a change that makes the agent
    sloppier draws rejections, those sessions leave the measurement, and the survivors show
    a better mean from a change that only did harm.
    """
    if not pending:
        return None, None, ""
    threshold = None
    if baseline is not None:
        threshold = round(float(baseline) * (1.0 - config.MIN_IMPROVEMENT), 1)
    if (base_valid_rate is not None and valid_rate is not None
            and valid_rate < base_valid_rate - config.VALID_RATE_TOLERANCE):
        return "revert", threshold, (
            f"valid rate fell from {base_valid_rate:.2f} to {valid_rate:.2f}: more "
            f"sessions drew a human intervention than before, and those sessions left "
            f"the measurement rather than entering it")
    if baseline is None or observed is None:
        return "revert", threshold, (
            "no comparable measurement for the batch that should have judged this change")
    if observed <= threshold:
        return "keep", threshold, (
            f"observed {observed} at or under the {threshold} bar "
            f"(baseline {baseline}, {config.MIN_IMPROVEMENT:.0%} required)")
    return "revert", threshold, (
        f"observed {observed} above the {threshold} bar "
        f"(baseline {baseline}, {config.MIN_IMPROVEMENT:.0%} required)")


def build_payload(agent, scored, excluded, memory_docs, log, pending, peers=None,
                  baseline=None, valid_rate=None, base_valid_rate=None):
    """The dict json.dumps'd into the analyst's user message.

    ``scored`` is the measurement from a SEPARATE call that saw only transcripts — no log,
    no pending change, no memory store — so the party being graded did not set its own
    difficulty. ``excluded`` are sessions that are evidence but never a number.
    """
    payload = {
        "agent": {"name": agent.get("name"),
                  "description": agent.get("description"),
                  "trigger": agent.get("trigger")},
        "min_improvement": config.MIN_IMPROVEMENT,
        "scored": scored or {},
        "baseline_performance": baseline,
        "valid_rate": valid_rate,
        "baseline_valid_rate": base_valid_rate,
        "excluded": list(excluded or []),
        "memory": {path: ("(unavailable)" if text is None else text)
                   for path, text in (memory_docs or {}).items()},
        "log": log or {"kept": [], "retryable": [], "banned": []},
        "pending": pending,
    }
    if peers:
        payload["peers"] = list(peers)
    return payload


def judge_and_propose(agent, scored, excluded, memory_docs, log, pending, model_fn,
                      peers=None, baseline=None, valid_rate=None, base_valid_rate=None):
    """ONE analyst call: judge the pending change, then propose at most one new one.

    Returns ``{"analysis", "verdict", "change", "blocked_on"}`` with the change's docs and
    deletes already sanitized, or None when the call or the parse failed. Never raises.

    ``verdict`` here is the MODEL'S stated answer and is recorded for comparison; the
    decision that executes comes from ``decide`` above.
    """
    user = json.dumps(build_payload(agent, scored, excluded, memory_docs, log, pending,
                                    peers=peers, baseline=baseline,
                                    valid_rate=valid_rate,
                                    base_valid_rate=base_valid_rate),
                      ensure_ascii=False, default=str)
    try:
        reply = model_fn(SYSTEM_PROMPT, user)
    except Exception as e:                                 # noqa: BLE001 — best-effort, but
        print(f"[learn] analysis call failed: {type(e).__name__}: {str(e)[:200]}",
              flush=True)
        return None
    obj = _parse_json_object(reply)
    if not obj:
        print(f"[learn] analysis reply did not parse as JSON. "
              f"Tail: {str(reply or '').rstrip()[-160:]!r}", flush=True)
        return None
    change = obj.get("change")
    if isinstance(change, dict):
        docs = _sanitize_docs(change.get("docs") or {})
        change = {"change_id": str(change.get("change_id") or "").strip()[:64],
                  "summary": str(change.get("summary") or "").strip(),
                  "technique": str(change.get("technique") or "").strip(),
                  "rationale": str(change.get("rationale") or "").strip(),
                  "peer_informed": str(change.get("peer_informed") or "").strip(),
                  "retry_of": str(change.get("retry_of") or "").strip(),
                  "docs": docs,
                  "delete": _sanitize_deletes(change.get("delete") or [], docs)}
        if not change["change_id"] or not (change["docs"] or change["delete"]):
            # A change that names nothing to write and nothing to remove is not a change.
            change = None
    else:
        change = None
    verdict = obj.get("verdict") if isinstance(obj.get("verdict"), dict) else None
    return {"analysis": str(obj.get("analysis") or "").strip(),
            "verdict": verdict,
            "change": change,
            "blocked_on": str(obj.get("blocked_on") or "").strip()}


def _doc_path(path):
    """A model-supplied path normalised to a store path, or None if it is not one.

    Tolerates a missing leading slash (the model writes "lessons_learned.md" as often
    as "/lessons_learned.md") and rejects everything else on shape: a traversal
    segment, a nested directory, a name that is not markdown, an absurd length. The
    engine chooses WHICH documents exist; it does not get to choose whether a path is
    a path."""
    p = str(path or "").strip()
    if not p:
        return None
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > MAX_DOC_PATH or ".." in p:
        return None
    if p in config.UNWRITABLE_DOCS:
        # The status document. Enforced here rather than argued in the prompt: it is the
        # one file with a second author (the agent rewrites it every session) and the one
        # whose loss reaches a customer, since its "Already handled" ledger is what stops
        # a finished email being sent twice. A path the engine cannot produce needs no
        # invariant over its contents.
        return None
    return p if _PATH_OK.match(p) else None


def _sanitize_docs(docs):
    """The documents this cycle may write: any valid store path, with non-empty string
    content, clipped to the size contract. Returns ``{path: content}``.

    No longer a whitelist — the engine owns the store (see the module docstring) — but
    still a gate: a path that is not a path, or content that is not a non-empty string,
    is dropped rather than handed to write_memory. An empty document is silently a
    DELETED one from the agent's point of view, so it must come through the delete
    channel where it is deliberate, not through a write that happened to be blank."""
    out = {}
    if not isinstance(docs, dict):
        return out
    for path, content in docs.items():
        p = _doc_path(path)
        if not p:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        out[p] = content[:config.LEARNING_DOC_CLIP]
    return out


def _sanitize_deletes(paths, written):
    """The documents this cycle may delete: valid store paths, minus any it is also
    writing in the same answer.

    A path in both lists is a contradiction the model has to lose: applying them in
    either order gives a different store, and the write is the one carrying content, so
    the write survives and the delete is dropped. Returns a list, order preserved,
    de-duplicated."""
    out, seen = [], set()
    for path in (paths or []):
        p = _doc_path(path)
        if not p or p in written or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out
