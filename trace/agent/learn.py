"""Learning engine — periodic Fable-driven self-improvement (learning cycles).

Every ``config.LEARNING_CYCLE_SESSIONS`` completed sessions, ONE ``model_fn(system,
user) -> str`` call reviews the batch — each session's compacted transcript, outcome
status, and token cost — together with the agent's memory documents and the outcomes
of prior cycles, then returns a full replacement ``lessons_learned.md`` aimed at
making future sessions more accurate and more token-efficient. Deterministic
parsing; best-effort — any failure returns None so a bad call never blocks a run.

Only ``lessons_learned.md`` may be rewritten: ``current_workflow_status.md`` is
agent-owned (rewritten at end of every session) and ``shared_lessons.md`` is owned by
the guidance path (rewritten before every run) — an edit to either would be clobbered
or clobber. ``_sanitize_docs`` enforces that whitelist in code, whatever the model
answered, because the transcripts it reads carry external content (a prompt-injection
surface) and whatever this engine writes steers every future session.
"""

import json

from . import activity, config, model_call
from .configgen import _parse_json_object
from .learn_prompt import SYSTEM_PROMPT

DEFAULT_MODEL = config.LEARNING_MODEL

# The one document a learning cycle may rewrite (see module docstring).
EDITABLE_DOCS = (config.MEMORY_LESSONS_DOC,)


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


def build_payload(agent, session_records, memory_docs, prior_cycles):
    """The dict json.dumps'd into the user message. ``memory_docs`` maps doc path
    -> text or None (None becomes "(unavailable)" so the model reasons without it
    rather than treating an unreadable file as empty); ``prior_cycles`` is the
    efficiency feedback loop — earlier analyses with their batches' average
    tokens per session."""
    return {
        "agent": {"name": agent.get("name"),
                  "description": agent.get("description"),
                  "trigger": agent.get("trigger")},
        "sessions": list(session_records),
        "memory": {path: ("(unavailable)" if text is None else text)
                   for path, text in (memory_docs or {}).items()},
        "prior_cycles": list(prior_cycles or []),
    }


def _sanitize_docs(docs):
    """Enforce the edit whitelist in code, whatever the model said: keep only
    EDITABLE_DOCS (tolerating a missing leading slash), with non-empty string
    content, clipped to the size contract. Returns ``{path: content}``."""
    out = {}
    if not isinstance(docs, dict):
        return out
    for path, content in docs.items():
        p = str(path or "")
        if not p.startswith("/"):
            p = "/" + p
        if p not in EDITABLE_DOCS:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        out[p] = content[:config.LEARNING_DOC_CLIP]
    return out


def analyze_batch(agent, session_records, memory_docs, prior_cycles, model_fn):
    """ONE analysis call over the batch. Returns ``{"analysis": str, "docs":
    {path: content}}`` with docs already sanitized (possibly empty — a valid
    "nothing to change"), or None when the call/parse failed. Never raises.

    The distinction matters to the caller: None records NO cycle, so the same
    batch is retried later; an empty-docs result IS recorded, so a clean
    "nothing to change" still resets the sessions-since-last-cycle counter.
    """
    if not session_records:
        return None
    user = json.dumps(build_payload(agent, session_records, memory_docs, prior_cycles),
                      ensure_ascii=False, default=str)
    try:
        obj = _parse_json_object(model_fn(SYSTEM_PROMPT, user))
    except Exception as e:                                 # noqa: BLE001 — best-effort, but
        # say WHY: a reason-less "analysis failed" line cost a debugging round.
        print(f"[learn] analysis call failed: {type(e).__name__}: {str(e)[:200]}",
              flush=True)
        return None
    if not obj:
        return None
    return {"analysis": str(obj.get("analysis") or "").strip(),
            "docs": _sanitize_docs(obj.get("docs") or {})}
