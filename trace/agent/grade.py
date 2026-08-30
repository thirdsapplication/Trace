"""Grading — what happens to the attempt that was pending, computed in code.

    improvement = (baseline - observed) / baseline        (lower tokens is better)

    A   improvement >= 3x the bar        kept
    B   2x <= improvement <  3x          kept
    C   1x <= improvement <  2x          kept
    D    0 <  improvement <  1x          reverted, retryable once
    F   improvement <= 0                 reverted, never repeated

A D and an F are both reverts and they are NOT the same evidence. A D is a technique that
worked and underdelivered; an F is one that backfired. Collapsing them throws away the
distinction that matters most when another agent reads the record, which is the whole
point of writing records down.

Everything here is arithmetic. The engine authored the change it is being asked to grade,
and the one pen a graded party must never hold is the grade. Its own stated verdict is
recorded beside this one and a disagreement is worth a log line, but this is what executes.
"""

from . import config

KEPT, REVERTED = "kept", "reverted"
# Boundary tolerance: see grade_for.
_EPS = 1e-9
# Grades that keep the change. Ordered best first, which is also the order the bar
# multiples are tested in.
KEEP_GRADES = ("A", "B", "C")
REVERT_GRADES = ("D", "F")


def improvement(baseline, observed):
    """The fraction the score fell by, or None when either end is missing.

    Positive means cheaper, which is better. A baseline of zero has no fraction to
    compute and reads as unmeasured rather than as an infinite win.
    """
    try:
        base, obs = float(baseline), float(observed)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    return (base - obs) / base


def grade_for(imp, bar=None):
    """The letter, or None when there was no measurement to grade."""
    if imp is None:
        return None
    bar = config.MIN_IMPROVEMENT if bar is None else float(bar)
    if imp <= 0:
        return "F"
    if bar <= 0:                       # a bar of zero would make every gain an A
        return "A"                     # -- degenerate, but never silently a divide
    # In multiples of the bar, with a tolerance, because the boundaries are exactly where
    # binary floating point lands wrong: 3 * 0.10 is 0.30000000000000004, so an
    # improvement of exactly three times the bar graded B until this was written down.
    ratio = imp / bar
    if ratio >= 3 - _EPS:
        return "A"
    if ratio >= 2 - _EPS:
        return "B"
    if ratio >= 1 - _EPS:
        return "C"
    return "D"


def outcome_for(grade):
    """kept for A/B/C, reverted for D/F and for an ungradeable attempt.

    An attempt nobody could measure is REVERTED, not kept: a change that cannot be shown
    to have helped must not stay in the store on the strength of having been proposed.
    """
    return KEPT if grade in KEEP_GRADES else REVERTED


def judge(pending, observed, baseline, bar=None):
    """Grade the pending attempt. Returns None when nothing is pending.

    ``{improvement, grade, outcome, baseline, observed, bar, reason}``. The reason is
    written for a human reading the log, and names the numbers rather than the verdict.
    """
    if not pending:
        return None
    bar = config.MIN_IMPROVEMENT if bar is None else float(bar)
    imp = improvement(baseline, observed)
    grade = grade_for(imp, bar)
    out = {"improvement": None if imp is None else round(imp, 4),
           "grade": grade, "outcome": outcome_for(grade),
           "baseline": baseline, "observed": observed, "bar": bar}
    if imp is None:
        out["reason"] = ("no comparable measurement for the group that should have "
                         "judged this attempt")
    else:
        out["reason"] = (f"{imp:+.1%} against a {bar:.0%} bar "
                         f"(baseline {baseline}, observed {observed}) — grade {grade}")
    return out


def _grade_of_record(a):
    """The grade an attempt written before grades existed would have got.

    Rows recorded by the previous engine carry the numbers and no letter. Reading them
    all as retryable would file an attempt that made things measurably WORSE as a
    near-miss and invite the engine to try it again, which is the exact confusion grades
    were added to remove. The numbers were always there; only the letter is new.
    """
    imp = improvement(a.get("baseline"), a.get("observed"))
    if imp is None:
        return None
    bar = a.get("threshold")
    try:                          # threshold was stored as the absolute bar, not a ratio
        bar = (float(a["baseline"]) - float(bar)) / float(a["baseline"]) if bar else None
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        bar = None
    return grade_for(imp, bar if bar and bar > 0 else None)


def log_lists(attempts):
    """This agent's own record, split into what the engine may do with each entry.

    - kept       in the store and working; techniques worth extending
    - retryable  a D, or an attempt nobody could measure. May be proposed once more,
                 with a stated reason why it clears the bar this time
    - banned     an F, or a D that has already been retried once. Permanent

    An entry with an outcome this module does not recognise is IGNORED rather than
    filed somewhere plausible: a failure recorded as a near-miss is worse than one
    the engine never sees.
    """
    kept, retryable, banned = [], [], []
    retried = {str(a.get("retry_of") or "") for a in (attempts or []) if a.get("retry_of")}
    for a in (attempts or []):
        if not isinstance(a, dict):
            continue
        outcome, grade = a.get("outcome"), a.get("grade") or _grade_of_record(a)
        if outcome == KEPT:
            kept.append(a)
        elif outcome == REVERTED:
            # An F is permanent. So is a D whose one retry has already been spent.
            if grade == "F" or str(a.get("attempt_id") or "") in retried:
                banned.append(a)
            else:
                retryable.append(a)
    return {"kept": kept, "retryable": retryable, "banned": banned}
