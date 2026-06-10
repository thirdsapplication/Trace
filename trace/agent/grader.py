"""P8 — grader (walled off from the Compiler, like the discovery Scorer).

Reads ONLY the spec's acceptance criteria and the sandbox run's outputs, and shares
no prompt text or code with the Compiler. Produces a per-criterion verdict
(pass | fail | not_testable_in_sandbox) with evidence. Criteria that require a live
external service to verify are marked not-testable, never silently passed (P8). The
gate is demonstrably capable of failing (a contradicted criterion -> fail ->
no deploy recommendation).
"""

import re

# Grader's OWN prompt — deliberately not imported from anywhere the Compiler touches.
GRADER_SYSTEM = (
    "You grade whether an automation agent's SANDBOX run satisfied ONE acceptance "
    "criterion. The run had NO external connections; any outbound action (sending an "
    "email, posting a message) appears as TEXT in the transcript rather than really "
    "happening. Rules:\n"
    "- If verifying the criterion would require a live external service (e.g. an email "
    "actually delivered, a message actually posted, a row written to a live system), "
    "answer NOT_TESTABLE.\n"
    "- Otherwise answer PASS if the transcript and output files clearly satisfy it, or "
    "FAIL if they contradict it or omit it.\n"
    "Answer with one word — PASS, FAIL, or NOT_TESTABLE — then a one-line reason."
)


def parse_acceptance_criteria(spec_md):
    """Extract the acceptance-criteria bullet list from a spec's section 3, stripping
    the trailing italic provenance tags (e.g. ``_(user-specified)_``)."""
    crit, in_sec = [], False
    for ln in (spec_md or "").splitlines():
        s = ln.strip()
        if re.match(r"^##\s*3\.", s):
            in_sec = True
            continue
        if in_sec and s.startswith("## "):
            break
        if in_sec and s.startswith("- "):
            text = re.sub(r"\s*_\([^)]*\)_\s*$", "", s[2:]).strip()
            if text and not text.startswith("_("):
                crit.append(text)
    return crit


def _parse_verdict(text):
    head = (text or "").strip().upper()
    first = head.split("\n", 1)[0]
    if "NOT_TESTABLE" in first or "NOT TESTABLE" in first:
        v = "not_testable_in_sandbox"
    elif first.startswith("FAIL"):
        v = "fail"
    elif first.startswith("PASS"):
        v = "pass"
    else:
        v = "not_testable_in_sandbox"
    return {"verdict": v, "evidence": (text or "").strip()[:400]}


def _default_judge(model="claude-sonnet-4-6"):
    import anthropic
    client = anthropic.Anthropic()

    def judge(criterion, transcript, output_files):
        files = "\n\n".join(f"--- OUTPUT FILE {n} ---\n{str(c)[:3000]}"
                            for n, c in (output_files or {}).items()) or "(no output files)"
        prompt = (f"ACCEPTANCE CRITERION:\n{criterion}\n\n"
                  f"AGENT RUN TRANSCRIPT:\n{(transcript or '')[:9000]}\n\n"
                  f"OUTPUT FILES:\n{files}")
        resp = client.messages.create(model=model, max_tokens=150, temperature=0,
                                      system=GRADER_SYSTEM,
                                      messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return _parse_verdict(text)

    return judge


def grade(criteria, run_outputs, judge_fn=None):
    """Grade a run's outputs against the criteria. ``run_outputs`` =
    ``{transcript, output_files, stalled, asked_question}``. ``judge_fn(criterion,
    transcript, output_files) -> {verdict, evidence}`` is injectable for offline tests."""
    if judge_fn is None:
        judge_fn = _default_judge()
    transcript = run_outputs.get("transcript", "")
    outputs = run_outputs.get("output_files", {})

    results = []
    for c in criteria:
        r = judge_fn(c, transcript, outputs)
        results.append({"criterion": c, "verdict": r.get("verdict", "not_testable_in_sandbox"),
                        "evidence": r.get("evidence", "")})

    passed = sum(1 for r in results if r["verdict"] == "pass")
    failed = sum(1 for r in results if r["verdict"] == "fail")
    not_testable = sum(1 for r in results if r["verdict"] == "not_testable_in_sandbox")
    stalled = bool(run_outputs.get("stalled"))
    asked = bool(run_outputs.get("asked_question"))

    return {
        "criteria_total": len(criteria),
        "passed": passed,
        "failed": failed,
        "not_testable_in_sandbox": not_testable,
        "results": results,
        "zero_coaching": {
            "stalled": stalled,
            "asked_question": asked,
            "note": "Only the trigger message was sent; no corrective/steering messages.",
        },
        # a contradicted (failing) criterion, a stall, or no passing evidence blocks the
        # recommendation — the gate must be able to fail.
        "deploy_recommended": (failed == 0 and passed > 0 and not stalled),
    }
