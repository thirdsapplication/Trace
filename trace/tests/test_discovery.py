import importlib
import json
import os
import tempfile
import unittest

engine = importlib.import_module("trace.discovery.discover")
from trace.discovery.discover import _parse_json_object, discover
from trace.discovery import validate
from trace.discovery.prompt import SYSTEM_PROMPT

FIXTURES = os.path.join(os.path.dirname(engine.__file__), "fixtures")
MARCY = os.path.join(FIXTURES, "marcy_event_log.json")


def write_log(events):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(events, f)
    return path


class RenderTest(unittest.TestCase):
    def test_render_includes_id_confidence_and_detail(self):
        events = [{"id": "e001", "ts_start": "t", "app": "Mail", "window": "Inbox",
                   "action": "read_email", "detail": "read a thing", "confidence": "high"}]
        line = engine.render_event_log(events)
        self.assertIn("e001", line)
        self.assertIn("high", line)
        self.assertIn("read_email", line)
        self.assertIn("read a thing", line)

    def test_whole_log_one_call_no_chunking(self):
        # the user content contains every event id; there is no per-chunk splitting
        events = [{"id": f"e{i:03d}", "ts_start": "t", "app": "A", "window": "W",
                   "action": "navigate", "detail": "x", "confidence": "high"}
                  for i in range(1, 51)]
        content = engine._build_user_content(events)
        for i in (1, 25, 50):
            self.assertIn(f"e{i:03d}", content)


class ParseTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(_parse_json_object('{"bundles":[]}'), {"bundles": []})

    def test_strips_fence(self):
        self.assertEqual(_parse_json_object('```json\n{"a":1}\n```'), {"a": 1})

    def test_extracts_object_from_prose(self):
        self.assertEqual(_parse_json_object('Sure: {"a":1} done'), {"a": 1})

    def test_garbage_returns_empty(self):
        self.assertEqual(_parse_json_object("not json"), {})


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.ids = {"e001", "e002", "e003"}
        self.report = validate.DiscoveryReport()

    def test_drops_unknown_evidence_and_flags(self):
        obj = {"bundles": [{"name": "x", "evidence_event_ids": ["e001", "e999"],
                            "confidence": "high"}]}
        out = validate.validate_discovery(obj, self.ids, self.report)
        self.assertEqual(out["bundles"][0]["evidence_event_ids"], ["e001"])
        self.assertTrue(self.report.errors)

    def test_assigns_ids_and_normalizes_confidence(self):
        obj = {"bundles": [{"name": "a", "evidence_event_ids": ["e001"], "confidence": "certain"},
                           {"name": "b", "evidence_event_ids": ["e002"], "confidence": "low"}],
               "anti_bundles": [{"name": "c", "evidence_event_ids": ["e003"], "confidence": "high"}]}
        out = validate.validate_discovery(obj, self.ids, self.report)
        self.assertEqual([b["id"] for b in out["bundles"]], ["B1", "B2"])
        self.assertEqual(out["bundles"][0]["confidence"], "low")  # repaired
        self.assertEqual(out["anti_bundles"][0]["id"], "A1")

    def test_carries_rationale_and_defaults_when_absent(self):
        # Spec 5: per-candidate rationale is carried for both groups; absent -> "".
        obj = {"bundles": [{"name": "a", "evidence_event_ids": ["e001"], "confidence": "high",
                            "rationale": "Same steps every time."}],
               "anti_bundles": [{"name": "c", "evidence_event_ids": ["e003"], "confidence": "high"}]}
        out = validate.validate_discovery(obj, self.ids, self.report)
        self.assertEqual(out["bundles"][0]["rationale"], "Same steps every time.")
        self.assertEqual(out["anti_bundles"][0]["rationale"], "")   # defaulted, present in contract

    def test_dedupes_evidence(self):
        obj = {"bundles": [{"name": "a", "evidence_event_ids": ["e001", "e001", "e002"],
                            "confidence": "high"}]}
        out = validate.validate_discovery(obj, self.ids, self.report)
        self.assertEqual(out["bundles"][0]["evidence_event_ids"], ["e001", "e002"])

    def test_non_object_returns_empty_shape(self):
        out = validate.validate_discovery([], self.ids, self.report)
        self.assertEqual(out, validate.empty_discovery())

    def test_always_has_contract_keys(self):
        out = validate.validate_discovery({}, self.ids, self.report)
        for k in ("bundles", "anti_bundles", "unclustered_or_noise_event_ids", "notes"):
            self.assertIn(k, out)


class DiscoverPipelineTest(unittest.TestCase):
    def test_discover_with_stub_validates_output(self):
        canned = {
            "bundles": [{"id": "B1", "name": "Intake", "description": "d", "trigger": "t",
                         "steps": ["s"], "frequency": "2x",
                         "evidence_event_ids": ["e001", "e002", "e999"],  # e999 unknown
                         "why_automatable": "rules", "confidence": "high"}],
            "anti_bundles": [{"id": "A1", "name": "Judgment", "description": "d",
                              "why_judgment": "varies", "recommended_handling": "human",
                              "evidence_event_ids": ["e013"], "confidence": "high"}],
            "unclustered_or_noise_event_ids": ["e007"],
            "notes": "ok",
        }
        out, report = discover(MARCY, model_fn=lambda s, u: json.dumps(canned))
        self.assertEqual(out["bundles"][0]["id"], "B1")
        self.assertNotIn("e999", out["bundles"][0]["evidence_event_ids"])  # filtered
        self.assertEqual(out["anti_bundles"][0]["id"], "A1")
        self.assertTrue(any("e999" in e for e in report.errors))

    def test_empty_log_returns_empty_discovery(self):
        path = write_log([])
        try:
            out, _ = discover(path, model_fn=lambda s, u: "{}")
            self.assertEqual(out, validate.empty_discovery())
        finally:
            os.remove(path)

    def test_determinism_same_input_same_output(self):
        fn = lambda s, u: json.dumps({"bundles": [], "anti_bundles": [],
                                      "unclustered_or_noise_event_ids": [], "notes": ""})
        a, _ = discover(MARCY, model_fn=fn)
        b, _ = discover(MARCY, model_fn=fn)
        self.assertEqual(a, b)


class RobustnessInstructionTest(unittest.TestCase):
    """§7 behaviors must be explicit in the instructions."""

    def test_v2_fields_carry_through_only_when_present(self):
        """apps, grouping_rationale, unknowns and the anti-bundle reason are carried
        when the model wrote them and absent otherwise; reason.why fills why_judgment
        (what the Review UI renders) when the model wrote the reason but not the field;
        an object-shaped step is folded to one string with its branch condition."""
        from trace.discovery import validate as dv
        rep = dv.DiscoveryReport()
        out = dv.validate_discovery({
            "bundles": [{"name": "b", "steps": [{"n": 1, "text": "Enter it",
                                                 "branch_if": "the item is urgent"}],
                         "evidence_event_ids": ["e001"], "apps": ["Gmail", " "],
                         "grouping_rationale": "same record",
                         "unknowns": [{"what": "destination tab", "evidence": ["e001", "e999"]}],
                         "confidence": "high"},
                        {"name": "plain", "steps": ["s"], "evidence_event_ids": ["e001"],
                         "confidence": "high"}],
            "anti_bundles": [{"name": "a", "evidence_event_ids": ["e001"], "confidence": "low",
                              "reason": {"type": "judgment", "app": None, "step": "2",
                                         "why": "deciding the exception"}}],
        }, {"e001"}, rep)
        b, plain = out["bundles"]
        self.assertEqual(b["steps"], ["If the item is urgent: Enter it"])
        self.assertEqual(b["apps"], ["Gmail"])
        self.assertEqual(b["grouping_rationale"], "same record")
        self.assertEqual(b["unknowns"], [{"what": "destination tab", "evidence": ["e001"]}])
        for k in ("apps", "grouping_rationale", "unknowns"):
            self.assertNotIn(k, plain)
        a = out["anti_bundles"][0]
        self.assertEqual(a["reason"], {"type": "judgment", "app": None, "step": 2,
                                       "why": "deciding the exception"})
        self.assertEqual(a["why_judgment"], "deciding the exception")

    def test_connectable_apps_are_injected_from_the_connect_table(self):
        from trace.discovery import prompt as dp
        from trace.agent import config
        self.assertIn(dp.APPS_PLACEHOLDER, dp.SYSTEM_PROMPT)         # template
        filled = dp.system_prompt()
        self.assertNotIn(dp.APPS_PLACEHOLDER, filled)
        for label in {v["label"] for v in config.CONNECT_APP_TABLE.values()}:
            self.assertIn(f"- {label}", filled)
        custom = dp.system_prompt(["One", "Two"])
        self.assertIn("- One\n- Two", custom)

    def test_prompt_covers_imperfect_log_behaviors(self):
        from trace.agent.foundation import FOUNDATION
        self.assertTrue(SYSTEM_PROMPT.startswith(FOUNDATION))
        p = SYSTEM_PROMPT.lower()
        self.assertIn("duplicate", p)
        self.assertIn("conflicting", p)
        self.assertIn("low-confidence", p)
        self.assertIn("never invent", p)

    def test_engine_does_not_crash_on_messy_log(self):
        messy = [
            {"id": "e001", "ts_start": "t", "app": "Sheet", "window": "W",
             "action": "enter_data", "detail": "entered order 12 units", "confidence": "high"},
            {"id": "e002", "ts_start": "t", "app": "Sheet", "window": "W",
             "action": "enter_data", "detail": "entered order 12 units (duplicate)", "confidence": "high"},
            {"id": "e003", "ts_start": "t", "app": "Sheet", "window": "W",
             "action": "enter_data", "detail": "tracking T1 vs T2 conflict", "confidence": "low"},
        ]
        path = write_log(messy)
        try:
            out, _ = discover(path, model_fn=lambda s, u: json.dumps(
                {"bundles": [], "anti_bundles": [],
                 "unclustered_or_noise_event_ids": ["e001", "e002", "e003"], "notes": ""}))
            self.assertEqual(len(out["unclustered_or_noise_event_ids"]), 3)
        finally:
            os.remove(path)


class WallAndNeutralityTest(unittest.TestCase):
    """The engine must not reach the answer key, and the engine/prompt must carry no
    fixture- or answer-key-specific content."""

    def _src(self, modname):
        import importlib
        mod = importlib.import_module(modname)
        with open(mod.__file__, "r", encoding="utf-8") as f:
            return f.read().lower()

    def test_engine_modules_do_not_reach_answer_key_or_scorer(self):
        # The wall: engine code never imports the scorer, reads an answer-key file,
        # or names a fixture. (Docstrings may mention "answer key" in prose.)
        import re
        for m in ("trace.discovery.discover", "trace.discovery.prompt",
                  "trace.discovery.validate", "trace.discovery.cli"):
            src = self._src(m)
            self.assertNotIn("answer_key", src, m)          # no answer-key file token
            self.assertNotIn("marcy", src, m)               # no fixture name
            self.assertNotIn("recruiter", src, m)
            self.assertFalse(re.search(r"import\s+\w*scorer|from\s+\.scorer", src), m)

    def test_prompt_has_no_fixture_domain_terms(self):
        import re
        p = SYSTEM_PROMPT.lower()
        for term in ("marcy", "bistro", "reorder_list", "sku", "purchase order",
                     "ats", "recruiter", "invoice", "resume"):
            self.assertIsNone(re.search(rf"\b{re.escape(term)}\b", p), term)


if __name__ == "__main__":
    unittest.main()
