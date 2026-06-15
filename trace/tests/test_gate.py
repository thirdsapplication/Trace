"""Phase 3 — hidden logic gate + grader (offline, FakeClient + stub judge, two domains).

Covers: the unattended trigger->run->grade->verdict flow; behavioral pass vs a
fabricating run failing; env-needing criteria marked not_testable_here (never failed);
the deterministic overall safety-net; the trigger synthesized from one observed
instance with no hints; transcript completeness; and that the gate session is kept.
"""

import json
import unittest

from trace.agent import configgen, gate, grader
from trace.tests.test_configgen import _bundle, _load, _stub as _config_stub

# explicit criteria exercised by the stub judge
CRITERIA = [
    "The order values from the trigger are entered into the destination.",
    "A confirmation reply is produced for the customer.",
    "The entered rows match the destination sheet's column format.",   # env-needing
]


def _stub_judge(force_pass_overall=False):
    """Reads the rendered transcript: env/format criteria -> not_testable_here;
    fabrication -> fail; evidenced -> pass; else inconclusive. Returns JSON text."""
    def judge(criteria, transcript):
        t = transcript.lower()
        per = []
        for c in criteria:
            cl = c.lower()
            if any(w in cl for w in ("format", "layout", "column", "deliver")):
                v, r = "not_testable_here", "needs the real file"
            elif "fabricated" in t:
                v, r = "fail", "agent invented data / simulated the send"
            elif ("entered" in t and "enter" in cl) or ("reply" in t and "reply" in cl) \
                    or ("confirm" in t and "confirmation" in cl):
                v, r = "pass", "evidenced in the transcript"
            else:
                v, r = "inconclusive", "no evidence"
            per.append({"criterion": c, "verdict": v, "reason": r})
        overall = "pass" if force_pass_overall else (
            "fail" if any(p["verdict"] == "fail" for p in per) else "pass")
        return json.dumps({"per_criterion": per, "overall": overall, "notes": ""})
    return judge


class FakeClient:
    def __init__(self, mode="correct"):
        self.mode = mode
        self.sessions, self.deleted_sessions, self.messages = [], [], []
        self.agents, self.last_resources = [], None

    def create_or_reuse_agent(self, envelope):
        self.agents.append(envelope)
        return {"id": "agent_0", "name": envelope["name"], "reused": False}

    def create_environment(self, defaults=None):
        return "env_0"

    def create_session(self, agent_id, environment_id, resources, title=None):
        self.last_resources = resources
        sid = f"sess_{len(self.sessions)}"
        self.sessions.append(sid)
        return sid

    def send_trigger_and_collect(self, session_id, message):
        self.messages.append((session_id, message))
        if self.mode == "fabricate":
            return [{"type": "agent.message",
                     "text": "FABRICATED: invented order not in the trigger; pretending the "
                             "confirmation was sent without doing the entry.", "name": None},
                    {"type": "session.status_idle", "text": "", "name": None}]
        if self.mode == "timeout":
            return [{"type": "agent.message", "text": "working on it", "name": None},
                    {"type": "client.timeout", "text": "stopped after 240s", "name": None}]
        return [
            {"type": "agent.message", "text": "Read the trigger and extracted the order values.", "name": None},
            {"type": "agent.tool_use", "text": "", "name": "write_file"},
            {"type": "agent.message", "text": "Entered the order values into the destination record.", "name": None},
            {"type": "agent.message", "text": "Reply (as text): Confirming order. confirmation produced.", "name": None},
            {"type": "session.status_idle", "text": "", "name": None},
        ]

    def download_outputs(self, session_id, exclude_ids=None):
        if self.mode == "correct":
            return {"result.csv": "order_id,customer,total\nPO-1,Acme,30\n"}, []
        return {}, []

    def delete_session(self, sid):
        self.deleted_sessions.append(sid)


def _test_dict(domain="marcy"):
    event_log, ak = _load(domain)
    bundle = _bundle(ak["bundles"][0])
    compiled = configgen.compile_config(
        bundle, configgen.make_engine_inputs(bundle, event_log)[1], [], _config_stub())
    return bundle, event_log, compiled["test"]["dict"]


class GateTest(unittest.TestCase):
    def test_unattended_pass_keeps_session_and_advances(self):
        bundle, event_log, td = _test_dict()
        client = FakeClient("correct")
        v = gate.run_gate(bundle, td, CRITERIA, event_log, client, judge_fn=_stub_judge())
        self.assertEqual(v["overall"], "pass")
        self.assertTrue(v["advanced"])
        self.assertEqual(len(client.messages), 1)         # only the trigger, once
        self.assertEqual(client.last_resources, [])       # no uploads/mounts
        self.assertEqual(client.deleted_sessions, [])     # session KEPT
        self.assertEqual(v["session_id"], "sess_0")
        self.assertTrue(v["transcript"] and v["per_criterion"])

    def test_fabrication_fails_and_holds(self):
        bundle, event_log, td = _test_dict()
        v = gate.run_gate(bundle, td, CRITERIA, event_log, FakeClient("fabricate"),
                          judge_fn=_stub_judge())
        self.assertEqual(v["overall"], "fail")
        self.assertFalse(v["advanced"])
        self.assertTrue(any(p["verdict"] == "fail" for p in v["per_criterion"]))

    def test_env_needing_criterion_not_testable_never_fails(self):
        bundle, event_log, td = _test_dict()
        v = gate.run_gate(bundle, td, CRITERIA, event_log, FakeClient("correct"),
                          judge_fn=_stub_judge())
        fmt = next(p for p in v["per_criterion"] if "format" in p["criterion"].lower())
        self.assertEqual(fmt["verdict"], "not_testable_here")
        self.assertEqual(v["overall"], "pass")            # not_testable_here never fails

    def test_overall_safety_net_overrides_model(self):
        # judge claims overall pass but emits a per-criterion fail -> grade forces fail
        bundle, event_log, td = _test_dict()
        v = gate.run_gate(bundle, td, CRITERIA, event_log, FakeClient("fabricate"),
                          judge_fn=_stub_judge(force_pass_overall=True))
        self.assertEqual(v["overall"], "fail")

    def test_trigger_from_observed_instance_no_hints(self):
        event_log, ak = _load("marcy")
        bundle = _bundle(ak["bundles"][0])
        trig = gate.generate_test_trigger(bundle, event_log)
        # carries the first evidence event's observed content...
        self.assertIn("Alpha Bistro", trig)
        # ...but no step/criteria hints leaked in
        self.assertNotIn("Handle the inbound item", trig)
        self.assertNotIn("Requirements", trig)

    def test_transcript_completeness(self):
        events = [
            {"type": "agent.message", "text": "did a thing", "name": None},
            {"type": "agent.tool_use", "text": "", "name": "write_file"},
            {"type": "client.timeout", "text": "stopped after 240s", "name": None},
        ]
        t = gate.render_transcript(events, {"out.csv": "a,b\n1,2\n",
                                            "blob.bin": "x\x00\x00binary"})
        self.assertIn("did a thing", t)
        self.assertIn("[tool_use: write_file]", t)
        self.assertIn("[error: stopped after 240s]", t)   # incompleteness visible to grader
        self.assertIn("Output file out.csv:", t)
        self.assertIn("binary artifact", t)               # binary not dumped raw

    def test_runs_on_both_domains_unchanged(self):
        for dom in ("marcy", "recruiter"):
            bundle, event_log, td = _test_dict(dom)
            v = gate.run_gate(bundle, td, CRITERIA, event_log, FakeClient("correct"),
                              judge_fn=_stub_judge())
            self.assertIn(v["overall"], ("pass", "fail"))
            self.assertTrue(v["trigger"])

    def test_unparseable_grader_is_failsafe(self):
        bundle, event_log, td = _test_dict()
        v = gate.run_gate(bundle, td, CRITERIA, event_log, FakeClient("correct"),
                          judge_fn=lambda c, t: "not json at all")
        self.assertEqual(v["overall"], "fail")            # broken grader never advances
        self.assertFalse(v["advanced"])


if __name__ == "__main__":
    unittest.main()
