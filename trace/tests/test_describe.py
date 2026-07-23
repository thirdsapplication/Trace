"""Describe Engine (skip-recording path): typed description -> discovery contract.

Offline via a stub ``model_fn`` (the same injection idiom as the Discovery Engine
tests). Asserts the secondhand-input hard rules: hallucinated evidence ids are
stripped, blank input and workflow-free output fail loudly, and the prompt stays
domain-neutral.
"""

import json
import unittest

from trace.discovery import describe
from trace.discovery.describe_prompt import SYSTEM_PROMPT

CANNED = {
    "bundles": [{
        "id": "B1",
        "name": "Enter supplier orders",
        "description": "Read each new order email in Gmail and add a row in Google Sheets.",
        "trigger": "A new order email arrives.",
        "steps": ["Open the order email in Gmail",
                  "Add one row to the tracker in Google Sheets"],
        "frequency": "a few times a day",
        "evidence_event_ids": ["e001", "e002"],       # hallucinated — must be stripped
        "why_automatable": "Fixed steps per order.",
        "rationale": "The same steps repeat for every order.",
        "confidence": "medium",
    }],
    "anti_bundles": [{
        "id": "A1",
        "name": "Negotiate rush requests",
        "description": "Deciding whether to accept a rush order.",
        "why_judgment": "Each depends on the relationship.",
        "recommended_handling": "A person decides; the agent only flags.",
        "rationale": "Every rush request needs a human call.",
        "evidence_event_ids": ["e003"],               # hallucinated — must be stripped
        "confidence": "low",
    }],
    "unclustered_or_noise_event_ids": ["e004"],       # hallucinated — must be stripped
    "notes": "Assumed the tracker is one sheet.",
}


def _stub(out=CANNED):
    return lambda system, user: json.dumps(out)


class DescribeEngineTest(unittest.TestCase):
    def test_valid_output_normalized_and_evidence_stripped(self):
        discovery, report = describe.describe_workflows(
            "I read order emails in Gmail and log them in Google Sheets.",
            model_fn=_stub())
        self.assertEqual(len(discovery["bundles"]), 1)
        self.assertEqual(len(discovery["anti_bundles"]), 1)
        # no event log exists: every hallucinated evidence id is dropped
        self.assertEqual(discovery["bundles"][0]["evidence_event_ids"], [])
        self.assertEqual(discovery["anti_bundles"][0]["evidence_event_ids"], [])
        self.assertEqual(discovery["unclustered_or_noise_event_ids"], [])
        self.assertTrue(any("unknown evidence id" in e for e in report.errors))
        # contract shape survives (ids, apps named in text, notes)
        b = discovery["bundles"][0]
        self.assertEqual(b["id"], "B1")
        self.assertIn("Gmail", " ".join(b["steps"]))
        self.assertEqual(discovery["notes"], "Assumed the tracker is one sheet.")

    def test_fenced_json_parsed(self):
        fenced = lambda s, u: "```json\n" + json.dumps(CANNED) + "\n```"
        discovery, _ = describe.describe_workflows("orders in Gmail", model_fn=fenced)
        self.assertEqual(len(discovery["bundles"]), 1)

    def test_blank_description_raises_valueerror(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                describe.describe_workflows(bad, model_fn=_stub())

    def test_workflow_free_output_raises_runtimeerror(self):
        for garbage in ('not json at all', '{"bundles": [], "anti_bundles": []}'):
            with self.assertRaises(RuntimeError):
                describe.describe_workflows("some vague words",
                                            model_fn=lambda s, u: garbage)

    def test_user_content_carries_description(self):
        captured = {}
        def spy(system, user):
            captured["system"], captured["user"] = system, user
            return json.dumps(CANNED)
        describe.describe_workflows("I file weekly reports.", model_fn=spy)
        self.assertEqual(captured["system"], SYSTEM_PROMPT)
        self.assertIn("I file weekly reports.", captured["user"])

    def test_prompt_domain_neutral_and_secondhand_rules(self):
        low = SYSTEM_PROMPT.lower()
        for term in ("marcy", "recruiter", "riverbend", "po-1001"):
            self.assertNotIn(term, low)
        # the two hard rules of the secondhand path are stated
        self.assertIn('"evidence_event_ids" list MUST be []', SYSTEM_PROMPT)
        self.assertIn("VERBATIM", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
