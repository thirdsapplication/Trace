"""Turn an answered decision into a de-identified, reusable lesson (Gap #3 fleet learning).

One ``model_fn(system, user) -> str`` call decides whether the resolved edge case yields a
GENERAL rule that would help a similar agent, and if so writes it scrubbed of any
identifying specifics. Deterministic parsing; best-effort — any failure returns
``{"reusable": False, "lesson": None}`` so a bad call never blocks the run.

The generalized lesson is the ONLY artifact allowed to cross user boundaries (see
docs/decision_memory_design.md §5), so scrubbing matters: the prompt forbids names, emails,
amounts, and company-specific identifiers, replacing them with neutral placeholders.
"""

from .configgen import _parse_json_object

GENERALIZE_SYSTEM = (
    "You distill reusable lessons from how a human resolved an automation agent's "
    "question. You are given the QUESTION the agent asked when it got stuck, the CONTEXT "
    "it had found, and the human's ANSWER.\n"
    "Decide: does this resolution yield a GENERAL rule that would help a DIFFERENT agent "
    "facing a SIMILAR situation? A rule is reusable only if it generalizes beyond this one "
    "instance (a policy, a default, a judgment boundary). One-off, purely case-specific "
    "answers are NOT reusable.\n"
    "If reusable, write ONE clear sentence stating the rule — DE-IDENTIFIED: no names, "
    "email addresses, phone numbers, monetary amounts, order/record ids, or company- or "
    "person-specific identifiers. Replace any such specifics with neutral placeholders "
    "(e.g. 'the customer', 'the order', 'a standard timeframe').\n"
    "Respond with ONLY a JSON object: {\"reusable\": true|false, \"lesson\": \"<the "
    "de-identified rule, or null>\"}.")


SCRUB_VERIFY_SYSTEM = (
    "You are a privacy gate. You are given ONE generalized lesson that may be shared with "
    "OTHER users' automation agents. Decide if it is SAFE to share: it must be a general "
    "rule with NO identifying specifics — no personal names, email addresses, phone "
    "numbers, monetary amounts, order/record ids, account numbers, or company- or "
    "person-specific identifiers. When in doubt, it is NOT safe.\n"
    "Respond with ONLY a JSON object: {\"safe\": true|false}.")


def verify_lesson_scrub(lesson, model_fn):
    """True iff ``lesson`` is safe to share cross-user (no identifying specifics). A second,
    independent pass before a lesson can go global. FAILS CLOSED — returns False on empty
    input or any error, so an unverifiable lesson is never shared."""
    if not str(lesson or "").strip():
        return False
    try:
        obj = _parse_json_object(model_fn(SCRUB_VERIFY_SYSTEM, f"LESSON: {lesson}")) or {}
    except Exception:                                      # noqa: BLE001 — fail closed
        return False
    return bool(obj.get("safe"))


def generalize_decision(decision, model_fn):
    """Return ``{"reusable": bool, "lesson": str|None}`` for one answered decision.
    ``decision`` carries ``question``, ``answer``, and a ``context`` dict/str. Never raises."""
    q = str(decision.get("question") or "").strip()
    ans = str(decision.get("answer") or "").strip()
    ctx = decision.get("context")
    if isinstance(ctx, dict):
        ctx = str(ctx.get("context") or "")
    ctx = str(ctx or "").strip()
    if not q or not ans:
        return {"reusable": False, "lesson": None}
    user = f"QUESTION: {q}\nCONTEXT: {ctx}\nANSWER: {ans}"
    try:
        obj = _parse_json_object(model_fn(GENERALIZE_SYSTEM, user)) or {}
    except Exception:                                      # noqa: BLE001 — best-effort
        return {"reusable": False, "lesson": None}
    lesson = str(obj.get("lesson") or "").strip()
    reusable = bool(obj.get("reusable")) and bool(lesson)
    return {"reusable": reusable, "lesson": lesson or None}
