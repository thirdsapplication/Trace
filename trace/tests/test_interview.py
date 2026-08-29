"""Phase 2 — Structured Interview gap detector (offline, stub model_fn, two domains).

Covers the triage normalization (enum coercion, runtime-discoverable drop, cap+sort,
re-id), domain-neutrality across the Marcy and recruiter fixtures, and the load-bearing
end-to-end feed: an interview record's answers + skips flow into the Phase-1 config
engine's grounding (source: user_answer; top-level unresolved).
"""

import json
import os
import tempfile
import unittest

from trace.agent import configgen, interview
from trace.agent.interview_prompt import SYSTEM_PROMPT
from trace.tests.test_configgen import _bundle, _load, _stub as _config_stub


# 6 raw questions: mixed cost order, one missing id, one bogus lens/cost, one
# runtime_discoverable (drop), one empty question (drop).
CANNED_QUESTIONS = {"questions": [
    {"id": "x", "unknown": "tab", "lens": "capture_blind_spot",
     "why_it_matters": "wrong tab is irreversible", "question": "Which tab do new records go in?",
     "suggested_answers": ["the main tab", ""], "answer_type": "short_text",
     "cost_if_wrong": "low", "runtime_discoverable": False},
    {"unknown": "layout", "lens": "capture_blind_spot", "why_it_matters": "the layout bug",
     "question": "One row, or one row per item?", "cost_if_wrong": "high",
     "runtime_discoverable": False},
    {"id": "q?", "unknown": "conflict", "lens": "bogus", "why_it_matters": "which wins",
     "question": "If the source differs from the existing rows, which wins?",
     "cost_if_wrong": "weird", "runtime_discoverable": False},
    {"unknown": "columns", "lens": "capture_blind_spot", "why_it_matters": "readable live",
     "question": "What are the column names?", "cost_if_wrong": "high",
     "runtime_discoverable": True},                       # dropped (runtime-discoverable)
    {"unknown": "dupes", "lens": "undetermined_decision", "why_it_matters": "dup handling",
     "question": "What counts as a duplicate?", "cost_if_wrong": "medium",
     "runtime_discoverable": False},
    {"unknown": "empty", "question": "  ", "cost_if_wrong": "high"},   # dropped (no question)
]}


def _stub(out=CANNED_QUESTIONS):
    return lambda system, user: json.dumps(out)


class InterviewEngineTest(unittest.TestCase):
    def _questions(self, domain="marcy"):
        event_log, ak = _load(domain)
        bundle = _bundle(ak["bundles"][0])
        return interview.generate_questions(bundle, event_log, model_fn=_stub())["questions"]

    def test_normalization_drops_and_caps_and_sorts(self):
        qs = self._questions()
        # 6 raw -> drop runtime_discoverable + empty -> 4 kept
        self.assertEqual(len(qs), 4)
        self.assertLessEqual(len(qs), interview._MAX_QUESTIONS)
        self.assertEqual([q["cost_if_wrong"] for q in qs], ["high", "medium", "medium", "low"])
        self.assertEqual([q["id"] for q in qs], ["q1", "q2", "q3", "q4"])
        # the runtime-discoverable 'column names' question is gone
        self.assertFalse(any("column names" in q["question"] for q in qs))

    def test_enum_coercion_and_shape(self):
        qs = self._questions()
        for q in qs:
            self.assertIn(q["lens"], ("capture_blind_spot", "undetermined_decision"))
            self.assertIn(q["cost_if_wrong"], ("high", "medium", "low"))
            self.assertEqual(q["answer_type"], "short_text")
            self.assertFalse(q["runtime_discoverable"])
            self.assertTrue(q["question"].strip())
            self.assertIsInstance(q["suggested_answers"], list)
        # the bogus-lens question was coerced to undetermined_decision
        conflict = next(q for q in qs if "which wins" in q["question"])
        self.assertEqual(conflict["lens"], "undetermined_decision")
        self.assertEqual(conflict["cost_if_wrong"], "medium")
        # empty suggested answers are dropped
        tab = next(q for q in qs if "tab" in q["question"])
        self.assertEqual(tab["suggested_answers"], ["the main tab"])

    def test_domain_neutral(self):
        # same stub -> identical normalized questions on both domains (no domain
        # string leaks into code); prompt material differs (different observed events)
        m = self._questions("marcy")
        r = self._questions("recruiter")
        self.assertEqual(m, r)
        ev_m, ak_m = _load("marcy")
        ev_r, ak_r = _load("recruiter")
        wf_m, obs_m, _ = configgen.make_engine_inputs(_bundle(ak_m["bundles"][0]), ev_m)
        wf_r, obs_r, _ = configgen.make_engine_inputs(_bundle(ak_r["bundles"][0]), ev_r)
        self.assertNotEqual(interview.build_prompt_material(wf_m, obs_m),
                            interview.build_prompt_material(wf_r, obs_r))

    def test_defensive_parse_fenced(self):
        ev, ak = _load("marcy")
        b = _bundle(ak["bundles"][0])
        fenced = lambda s, u: "```json\n" + json.dumps(CANNED_QUESTIONS) + "\n```\ntrailing"
        qs = interview.generate_questions(b, ev, model_fn=fenced)["questions"]
        self.assertEqual(len(qs), 4)

    def test_no_questions_is_a_valid_answer(self):
        """A model that finds no real gap yields NO questions.

        This used to back-fill three canned questions, on the theory that the interview
        was mandatory. The gap-detector prompt already tells the model not to pad, so
        the padding was code overriding a correct answer — and three generic questions
        on a simple workflow teach the worker that the interview is boilerplate to click
        through, which is what makes them rush the interview that does matter.
        """
        ev, ak = _load("marcy")
        b = _bundle(ak["bundles"][0])
        qs = interview.generate_questions(b, ev, model_fn=lambda s, u: '{"questions": []}')["questions"]
        self.assertEqual(qs, [])

    def test_unusable_questions_are_not_replaced_with_filler(self):
        """Every question filtered out by normalization also yields none.

        The old fallback fired on this path too, so a model that returned only
        runtime-discoverable questions produced three unrelated canned ones.
        """
        ev, ak = _load("marcy")
        b = _bundle(ak["bundles"][0])
        raw = json.dumps({"questions": [
            {"question": "What columns does the destination sheet have?",
             "runtime_discoverable": True},
            {"question": "", "runtime_discoverable": False}]})
        qs = interview.generate_questions(b, ev, model_fn=lambda s, u: raw)["questions"]
        self.assertEqual(qs, [])

    def test_prompt_permits_an_empty_set(self):
        # The model has to be told, or it invents a question rather than return none.
        self.assertIn("NO MINIMUM", SYSTEM_PROMPT)

    def test_system_prompt_domain_neutral(self):
        low = SYSTEM_PROMPT.lower()
        for term in ("marcy", "recruiter", "gmail", "riverbend", "po-1001"):
            self.assertNotIn(term, low)

    def test_cap_doubled_to_ten(self):
        # 12 substantively distinct, well-formed questions -> capped at the doubled
        # limit (10), not the old 5
        self.assertEqual(interview._MAX_QUESTIONS, 10)
        prompts = [
            "Which destination receives the finished record?",
            "Should multiple line items become separate rows or stay combined?",
            "When the source disagrees with prior entries, whose value prevails?",
            "What marks an incoming item as already processed?",
            "Under what condition must a human approve before sending?",
            "What tone or signature belongs on outgoing replies?",
            "Which currency or unit applies when none is stated?",
            "How far back should the agent look for matching context?",
            "What deadline or schedule governs when this runs?",
            "Which sender domains are trusted without extra review?",
            "What fallback applies when a required field is missing?",
            "How should attachments be named when saved?",
        ]
        raw = {"questions": [
            {"unknown": f"u{i}", "lens": "undetermined_decision", "why_it_matters": "x",
             "question": p, "cost_if_wrong": "medium", "runtime_discoverable": False}
            for i, p in enumerate(prompts)]}
        ev, ak = _load("marcy")
        b = _bundle(ak["bundles"][0])
        qs = interview.generate_questions(b, ev, model_fn=_stub(raw))["questions"]
        self.assertEqual(len(qs), 10)
        self.assertEqual([q["id"] for q in qs], [f"q{i + 1}" for i in range(10)])

    def test_near_duplicate_questions_dropped(self):
        # two near-identical phrasings collapse to one; the distinct one survives
        raw = {"questions": [
            {"unknown": "tab", "lens": "capture_blind_spot", "why_it_matters": "x",
             "question": "Which tab do new records go into?", "cost_if_wrong": "high",
             "runtime_discoverable": False},
            {"unknown": "tab2", "lens": "capture_blind_spot", "why_it_matters": "x",
             "question": "What tab should new records go into?", "cost_if_wrong": "medium",
             "runtime_discoverable": False},
            {"unknown": "conflict", "lens": "undetermined_decision", "why_it_matters": "x",
             "question": "When the source disagrees with existing rows, which value wins?",
             "cost_if_wrong": "high", "runtime_discoverable": False},
        ]}
        ev, ak = _load("marcy")
        b = _bundle(ak["bundles"][0])
        qs = interview.generate_questions(b, ev, model_fn=_stub(raw))["questions"]
        texts = [q["question"] for q in qs]
        self.assertEqual(len(qs), 2)                          # the rephrase was dropped
        self.assertIn("Which tab do new records go into?", texts)   # higher-cost keeper
        self.assertTrue(any("which value wins" in t for t in texts))


class InterviewToConfigFeedTest(unittest.TestCase):
    """The load-bearing pipe: interview answers + skips -> Phase-1 grounding."""

    def _write(self, domain, answers, unresolved):
        event_log, ak = _load(domain)
        b = _bundle(ak["bundles"][0])
        b.update({"classification": "bundle", "disposition": "approve_for_build"})
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "reviewed_bundles.json"), "w") as f:
            json.dump({"bundles": [b], "anti_bundles": [], "notes": ""}, f)
        with open(os.path.join(d, "event_log.json"), "w") as f:
            json.dump(event_log, f)
        safe = "".join(c if c.isalnum() else "_" for c in b["id"])
        with open(os.path.join(d, f"interview_{safe}.json"), "w") as f:
            json.dump({"bundle_id": b["id"], "questions": [], "answers": answers,
                       "unresolved": unresolved, "status": "incomplete"}, f)
        return d, b["id"]

    def test_answers_and_unresolved_reach_grounding(self):
        for domain in ("marcy", "recruiter"):
            answers = [{"q": "q1", "question": "Which tab?", "answer": "Open_Orders"},
                       {"q": "q2", "question": "Conflict?", "answer": "Follow the source."}]
            d, bid = self._write(domain, answers, ["q3"])
            results, _ = configgen.generate_configs(
                os.path.join(d, "reviewed_bundles.json"),
                os.path.join(d, "event_log.json"), model_fn=_config_stub())
            g = next(r["grounding"] for r in results if r["built"])
            # the config CANNED sources a step to user_answer q1 -> flows through verbatim
            ua = [i for i in g["instructions"]
                  if isinstance(i["source"], dict) and i["source"].get("type") == "user_answer"]
            self.assertTrue(ua)
            self.assertFalse(ua[0]["observed"])
            self.assertIn(ua[0]["source"]["q"], ("q1", "q2"))
            # the skipped question is flagged top-level
            self.assertEqual(g["unresolved"], ["q3"])

    def test_missing_interview_file_no_unresolved_key(self):
        event_log, ak = _load("marcy")
        b = _bundle(ak["bundles"][0])
        b.update({"classification": "bundle", "disposition": "approve_for_build"})
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "reviewed_bundles.json"), "w") as f:
            json.dump({"bundles": [b], "anti_bundles": [], "notes": ""}, f)
        with open(os.path.join(d, "event_log.json"), "w") as f:
            json.dump(event_log, f)
        results, _ = configgen.generate_configs(
            os.path.join(d, "reviewed_bundles.json"),
            os.path.join(d, "event_log.json"), model_fn=_config_stub())
        g = next(r["grounding"] for r in results if r["built"])
        self.assertNotIn("unresolved", g)        # additive extension stays non-breaking


class UserDescriptionToInterviewTest(unittest.TestCase):
    """The worker's review-step correction reaches the gap detector's prompt material
    (so questions target the workflow the worker described), and only when present."""

    def test_correction_in_gap_material_only_when_present(self):
        ev, ak = _load("marcy")
        b = _bundle(ak["bundles"][0])
        wf, obs, _ = configgen.make_engine_inputs(b, ev)
        self.assertNotIn("user_description", interview.build_prompt_material(wf, obs))
        b["user_description"] = "I enter each tracking number into the destination record."
        wf2, obs2, _ = configgen.make_engine_inputs(b, ev)
        material = interview.build_prompt_material(wf2, obs2)
        self.assertIn("user_description", material)
        self.assertIn("I enter each tracking number into the destination record.", material)

    def test_system_prompt_declares_correction_authoritative(self):
        self.assertIn("user_description", SYSTEM_PROMPT)
        self.assertIn("AUTHORITATIVE", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()


class ExtraNotesTest(unittest.TestCase):
    """The optional free-text note: reaches the agent config, never gates the step."""

    def setUp(self):
        import tempfile
        from trace.ui import app as ui_app
        self.app = ui_app
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _rec(self, answers):
        return self.app._interview_record("B1", [{"id": "q1", "question": "Which tab?"}], answers)

    def test_note_does_not_count_as_an_unanswered_question(self):
        rec = self._rec([{"q": self.app.EXTRA_NOTES_ID, "question": self.app.EXTRA_NOTES_LABEL,
                          "answer": "skip anything from a personal address"}])
        self.assertEqual(rec["unresolved"], ["q1"])      # the real question is still open
        self.assertEqual(rec["status"], "incomplete")

    def test_note_alongside_answers_completes(self):
        rec = self._rec([{"q": "q1", "question": "Which tab?", "answer": "Open_Orders"},
                         {"q": self.app.EXTRA_NOTES_ID, "question": self.app.EXTRA_NOTES_LABEL,
                          "answer": "never touch the archive tab"}])
        self.assertEqual(rec["unresolved"], [])
        self.assertEqual(rec["status"], "complete")

    def test_note_reaches_the_agent_config(self):
        """configgen reads the record's `answers` list, so the note must live there —
        otherwise the field is decorative."""
        import json, os
        from trace.agent import configgen
        os.makedirs(os.path.join(self.root, "specs"), exist_ok=True)
        rec = self._rec([{"q": "q1", "question": "Which tab?", "answer": "Open_Orders"},
                         {"q": self.app.EXTRA_NOTES_ID, "question": self.app.EXTRA_NOTES_LABEL,
                          "answer": "never touch the archive tab"}])
        with open(os.path.join(self.root, "interview_B1.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)
        loaded = configgen._load_answers(self.root, "B1")
        self.assertIn("never touch the archive tab", json.dumps(loaded))
