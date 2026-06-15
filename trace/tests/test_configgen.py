"""Phase 1 — Config Engine (offline, stub model_fn, two domains).

The fixtures ship only event logs + answer keys ({id,intent,event_ids}), so each test
synthesizes a contract-shaped reviewed bundle from a fixture and runs it through the
engine with a stubbed model. Covers: byte-identical structural scaffolding across the
Marcy (restaurant-supply) and recruiter domains, the test/live variants, the fixed
system-prompt order, the appended canonical rule, grounding completeness, and that no
markdown is produced.
"""

import json
import os
import tempfile
import unittest

from trace.agent import config, configgen
from trace.artifact import review

FIX = os.path.join(os.path.dirname(__file__), "..", "discovery", "fixtures")


def _load(domain):
    with open(os.path.join(FIX, f"{domain}_event_log.json"), encoding="utf-8") as f:
        event_log = json.load(f)
    with open(os.path.join(FIX, f"{domain}_answer_key.json"), encoding="utf-8") as f:
        answer_key = json.load(f)
    return event_log, answer_key


def _bundle(akb, name=None):
    """A reviewed-bundle-shaped dict synthesized from an answer-key entry."""
    return {"id": akb["id"], "name": name or " ".join(akb["intent"].split()[:4]),
            "description": akb["intent"], "trigger": "A new instance of this workflow occurs.",
            "steps": ["Handle the inbound item.", "Record the result."],
            "evidence_event_ids": list(akb["event_ids"])}


# Canonical model output (content only); one source of each type. NO 'match existing
# examples' working rule -> the engine must append it.
CANNED = {
    "description": "Processes each inbound item end to end.",
    "role": "You process inbound items one at a time.",
    "tools_and_artifacts": ["Email", "Spreadsheet"],
    "steps": [
        {"text": "Open the inbound item and read its contents.",
         "source": {"type": "events", "ids": ["e001", "e002"]}},
        {"text": "Enter the item into the destination record.",
         "source": {"type": "user_answer", "q": "q1"}},
    ],
    "working_rules": [
        {"text": "Enter values exactly as on the source; never substitute.",
         "source": {"type": "events", "ids": ["e003"]}},
        {"text": "Where the source and destination conflict, follow the source.",
         "source": {"type": "user_answer", "q": "q2"}},
    ],
    "requirements": [
        {"text": "Each item is fully processed before the next begins.",
         "source": {"type": "discovery"}},
    ],
}

# Variant whose working rules already include a match-existing-examples rule.
CANNED_WITH_MATCH = json.loads(json.dumps(CANNED))
CANNED_WITH_MATCH["working_rules"].append(
    {"text": "Match the existing rows already in the destination and follow their format.",
     "source": {"type": "events", "ids": ["e003"]}})


def _stub(out=CANNED):
    return lambda system, user: json.dumps(out)


def _compile(domain, akb_index=0, name=None, model_out=CANNED, answers=None):
    event_log, ak = _load(domain)
    bundle = _bundle(ak["bundles"][akb_index], name=name)
    _, observed, _ = configgen.make_engine_inputs(bundle, event_log)
    return configgen.compile_config(bundle, observed, answers or [], _stub(model_out))


class ConfigGenTest(unittest.TestCase):
    # 1 + 2 -----------------------------------------------------------------
    def test_no_markdown_anywhere(self):
        out = _compile("marcy")
        self.assertIn("test", out)
        self.assertIn("live", out)
        self.assertIn("grounding", out)
        self.assertNotIn("markdown", out)
        self.assertNotIn("spec", out)

    def test_scaffolding_byte_identical_across_domains(self):
        # same bundle id+name + same model output -> the domain (different observed
        # events) must not leak into the output at all.
        m = _compile("marcy", name="Shared Workflow")
        r = _compile("recruiter", name="Shared Workflow")
        # force identical ids so even metadata matches
        self.assertEqual(_retag(m, "B"), _retag(r, "B"))

    def test_structural_fields_present_in_both_domains(self):
        for dom in ("marcy", "recruiter"):
            out = _compile(dom)
            test_yaml, live_yaml = out["test"]["yaml"], out["live"]["yaml"]
            self.assertIn(f"model: {config.DEFAULT_MODEL}", test_yaml)
            self.assertIn(f"type: {config.AGENT_TOOLSET_TYPE}", test_yaml)
            self.assertIn("skills: []", test_yaml)
            self.assertIn("mcp_servers: []", test_yaml)         # test: no MCP
            # live template carries the nominal Pipedream server (deploy bakes the
            # per-customer, per-app URLs at create time).
            self.assertIn(config.PIPEDREAM_MCP_BASE, live_yaml)
            self.assertIn(config.PIPEDREAM_MCP_NAME, live_yaml)
            self.assertIn(f"type: {config.LIVE_PERMISSION_POLICY}", live_yaml)

    # 3 ---------------------------------------------------------------------
    def test_test_vs_live_toolsets(self):
        out = _compile("recruiter")
        self.assertNotIn("mcp_toolset", out["test"]["yaml"])
        self.assertNotIn("pipedream", out["test"]["yaml"])
        self.assertIn("mcp_toolset", out["live"]["yaml"])
        self.assertEqual(out["test"]["dict"]["mcp_servers"], [])
        self.assertEqual(len(out["live"]["dict"]["mcp_servers"]), 1)

    # 4 ---------------------------------------------------------------------
    def test_system_text_fixed_order(self):
        env = configgen.compiler.validate_yaml(_compile("marcy")["test"]["yaml"])
        sys_text = env["system"]
        for marker in ("You work with:", "Steps:", "How to do the work:", "Requirements:"):
            self.assertIn(marker, sys_text)
        # markers appear in order; role precedes them; steps are 1-based
        order = [sys_text.index(m) for m in
                 ("You work with:", "Steps:", "How to do the work:", "Requirements:")]
        self.assertEqual(order, sorted(order))
        self.assertTrue(sys_text.startswith(CANNED["role"]))
        self.assertIn("1. Open the inbound item", sys_text)

    # 5 ---------------------------------------------------------------------
    def test_canonical_rule_appended_when_missing(self):
        out = _compile("marcy", model_out=CANNED)            # CANNED has no match rule
        self.assertIn(config.MATCH_EXAMPLES_RULE, out["test"]["yaml"])
        keys = [i["key"] for i in out["grounding"]["instructions"]]
        self.assertIn("rule_match_existing_examples", keys)
        entry = next(i for i in out["grounding"]["instructions"]
                     if i["key"] == "rule_match_existing_examples")
        self.assertFalse(entry["observed"])
        self.assertEqual(entry["source"], {"type": "discovery"})

    def test_canonical_rule_not_duplicated_when_present(self):
        out = _compile("marcy", model_out=CANNED_WITH_MATCH)
        # the model supplied its own match rule -> the canonical one is NOT appended
        keys = [i["key"] for i in out["grounding"]["instructions"]]
        self.assertNotIn("rule_match_existing_examples", keys)
        self.assertNotIn(config.MATCH_EXAMPLES_RULE, out["test"]["yaml"])
        self.assertIn("Match the existing rows already in the destination", out["test"]["yaml"])

    # 6 ---------------------------------------------------------------------
    def test_grounding_complete_and_well_formed(self):
        out = _compile("marcy", model_out=CANNED)
        instr = out["grounding"]["instructions"]
        # steps(2) + working_rules(2 + appended canonical = 3) + requirements(1) = 6
        self.assertEqual(len(instr), 6)
        self.assertEqual(out["grounding"]["bundle_id"], "AKB1")
        for it in instr:
            self.assertTrue(it["key"])
            self.assertTrue(str(it["text"]).strip())
            self.assertIn(it["source"]["type"], ("events", "discovery", "user_answer"))
            self.assertEqual(it["observed"], it["source"]["type"] == "events")
        # keys are unique; role/description/tools are NOT grounded
        self.assertEqual(len(set(i["key"] for i in instr)), len(instr))
        self.assertFalse(any("role" in i["text"].lower() and i["key"].startswith("desc")
                             for i in instr))

    # 7 ---------------------------------------------------------------------
    def test_empty_user_answers_runs_on_both_domains(self):
        for dom in ("marcy", "recruiter"):
            out = _compile(dom, answers=[])
            self.assertTrue(out["test"]["yaml"])
            self.assertTrue(out["live"]["yaml"])

    # 8 ---------------------------------------------------------------------
    def test_determinism(self):
        a = _compile("marcy")
        b = _compile("marcy")
        self.assertEqual(a["test"]["yaml"], b["test"]["yaml"])
        self.assertEqual(a["live"]["yaml"], b["live"]["yaml"])
        self.assertEqual(json.dumps(a["grounding"], sort_keys=True),
                         json.dumps(b["grounding"], sort_keys=True))

    def test_live_envelope_validates_test_has_no_mcp(self):
        out = _compile("recruiter")
        configgen.validate_config_envelope(out["test"]["dict"], "test")
        configgen.validate_config_envelope(out["live"]["dict"], "live")


class GenerateConfigsFileTest(unittest.TestCase):
    """The file-driven engine entrypoint over a synthesized reviewed file."""

    def _write_inputs(self, domain, dispositions=("approve_for_build",)):
        event_log, ak = _load(domain)
        bundles = []
        for i, disp in enumerate(dispositions):
            b = _bundle(ak["bundles"][i])
            b.update({"classification": "bundle", "disposition": disp})
            bundles.append(b)
        reviewed = {"bundles": bundles, "anti_bundles": [], "notes": ""}
        d = tempfile.mkdtemp()
        rp = os.path.join(d, "reviewed_bundles.json")
        ep = os.path.join(d, "event_log.json")
        with open(rp, "w") as f:
            json.dump(reviewed, f)
        with open(ep, "w") as f:
            json.dump(event_log, f)
        return rp, ep

    def test_generates_yaml_and_grounding_no_markdown(self):
        rp, ep = self._write_inputs("marcy")
        results, report = configgen.generate_configs(rp, ep, model_fn=_stub())
        built = [r for r in results if r["built"]]
        self.assertEqual(len(built), 1)
        r = built[0]
        self.assertTrue(r["test_yaml"] and r["live_yaml"])
        self.assertIsNotNone(r["grounding"])
        for r in results:
            self.assertNotIn("markdown", r)

    def test_rejected_bundle_not_built_and_model_not_called(self):
        rp, ep = self._write_inputs("recruiter", dispositions=("reject",))

        def boom(system, user):
            raise AssertionError("model_fn must not be called when nothing is approved")

        results, _ = configgen.generate_configs(rp, ep, model_fn=boom)
        self.assertTrue(all(not r["built"] for r in results))
        self.assertIsNone(results[0]["test_yaml"])


def _retag(out, bid):
    """Replace the bundle id in both yamls so two runs with different ids compare equal
    on everything structural."""
    test = out["test"]["yaml"].replace(out["test"]["dict"]["metadata"]["trace_bundle_id"] or "", bid)
    live = out["live"]["yaml"].replace(out["live"]["dict"]["metadata"]["trace_bundle_id"] or "", bid)
    return (test, live)


if __name__ == "__main__":
    unittest.main()
