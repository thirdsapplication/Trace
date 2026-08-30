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


# log_lists and decide moved to grade.py when grading became A-F. The verdict is
# arithmetic over one number now, and it belongs beside the grade table rather than
# beside the prompt that must not see it.


def build_payload(agent, observed, excluded, memory_docs, log, pending, peers=None,
                  baseline=None, valid_rate=None, base_valid_rate=None,
                  profiles=None, prefix=None, preconditions=None,
                  reverted_change=None, converged=False):
    """The dict json.dumps'd into the engine's user message.

    ``observed`` is the score: mean TOTAL TOKENS per valid session, computed in code. No
    model produced it and none may argue with it. ``profiles`` say where those tokens went
    per session, ``prefix`` says what share of the per-turn cost the engine can even reach,
    and ``excluded`` sessions are evidence that never becomes a number.

    ``reverted_change`` is present only when the pending attempt was graded D or F. By the
    time this payload is built the store has ALREADY been restored, so ``memory`` holds
    the state that will actually run and this field holds what was undone.
    """
    payload = {
        "agent": {"name": agent.get("name"),
                  "description": agent.get("description"),
                  "trigger": agent.get("trigger")},
        "min_improvement": config.MIN_IMPROVEMENT,
        "score": {"mean_total_tokens": observed,
                  "baseline_mean_total_tokens": baseline,
                  "sessions_valid": len(profiles or []),
                  "sessions_excluded": len(excluded or []),
                  "valid_rate": valid_rate,
                  "baseline_valid_rate": base_valid_rate,
                  "prefix": prefix or {}},
        "preconditions": preconditions or {},
        "sessions": list(profiles or []),
        "excluded": list(excluded or []),
        "memory": {path: ("(unavailable)" if text is None else text)
                   for path, text in (memory_docs or {}).items()},
        "attempts": log or {"kept": [], "retryable": [], "banned": []},
        "pending": pending,
    }
    if reverted_change:
        payload["reverted_change"] = reverted_change
    if converged:
        # Several attempts in a row reverted. The engine is told so plainly rather than
        # being left to infer it from the log, because "propose nothing" is the answer
        # here and it is the one answer a proposing engine will not reach on its own.
        payload["converged"] = True
    if peers:
        payload["peers"] = list(peers)
    return payload


def judge_and_propose(agent, observed, excluded, memory_docs, log, pending, model_fn,
                      peers=None, baseline=None, valid_rate=None, base_valid_rate=None,
                      profiles=None, prefix=None, preconditions=None,
                      reverted_change=None, converged=False):
    """ONE model call per cycle: explain the graded attempt, then propose at most one.

    Returns ``{"analysis", "attempt_review", "change", "blocked_on"}`` with the change's
    docs and deletes already sanitized, or None when the call or the parse failed. Never
    raises.

    The engine no longer decides anything. The grade was computed in code before this call
    and, when it was a revert, applied before it too — so what comes back is an
    explanation and a proposal, not a verdict.
    """
    user = json.dumps(build_payload(agent, observed, excluded, memory_docs, log, pending,
                                    peers=peers, baseline=baseline,
                                    valid_rate=valid_rate,
                                    base_valid_rate=base_valid_rate,
                                    profiles=profiles, prefix=prefix,
                                    preconditions=preconditions,
                                    reverted_change=reverted_change,
                                    converged=converged),
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
        change = {"change_id": str(change.get("change_id")
                                   or change.get("attempt_id") or "").strip()[:64],
                  "change_type": _change_type(change.get("change_type")),
                  "summary": str(change.get("summary")
                                 or change.get("description") or "").strip(),
                  "technique": str(change.get("technique") or "").strip(),
                  "rationale": str(change.get("rationale")
                                   or change.get("reasoning") or "").strip(),
                  "preconditions": str(change.get("preconditions") or "").strip(),
                  "peer_informed": str(change.get("peer_informed")
                                       or change.get("fleet_informed") or "").strip(),
                  "retry_of": str(change.get("retry_of") or "").strip(),
                  "docs": docs,
                  "delete": _sanitize_deletes(change.get("delete") or [], docs)}
        if not change["change_id"] or not (change["docs"] or change["delete"]):
            # A change that names nothing to write and nothing to remove is not a change.
            change = None
    else:
        change = None
    review = obj.get("attempt_review")
    if not isinstance(review, dict):
        review = obj.get("verdict") if isinstance(obj.get("verdict"), dict) else None
    return {"analysis": str(obj.get("analysis") or "").strip(),
            "attempt_review": review,
            "change": change,
            "blocked_on": str(obj.get("blocked_on") or "").strip()}


CHANGE_TYPES = ("prune", "restructure", "add_lesson", "tool_recipe", "early_exit",
                "reorder", "output_brevity")


def _change_type(value):
    """One of the known types, or "other". A free-text type would make the fleet's
    aggregate outcomes by type uncountable, which is the one thing they are for."""
    v = str(value or "").strip().lower()
    return v if v in CHANGE_TYPES else "other"


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
