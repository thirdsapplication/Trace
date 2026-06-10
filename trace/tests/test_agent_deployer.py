"""Stage 4 — Test Deployer (offline, stub API client + stub judge).

Verifies the full sandbox orchestration without a real platform: files uploaded +
mounted, one session per instance, ONLY the trigger sent (zero coaching), outputs
downloaded, session + files deleted (retention clean), and grading against the
spec's own criteria — including the gate's ability to fail.
"""

import unittest

from trace.agent import deployer
from trace.agent.compiler import compile_agent
from trace.tests.test_agent import BUNDLE, EVENT_LOG, SPEC_MD

ENV_RECORD = {"artifacts": [
    {"role": "input", "uploaded": True, "file_ref": "/s/PO.pdf", "user_instruction": "the PO"},
    {"role": "output", "uploaded": True, "file_ref": "/s/SHEET.xlsx", "user_instruction": "the live tab"}]}


class FakeClient:
    """Records calls; simulates a successful single-instance run that echoes the
    preflight header, appends exactly one row, and emits a confirmation."""

    def __init__(self, ask=False, stall=False, leftover=False):
        self.ask, self.stall, self.leftover = ask, stall, leftover
        self.uploaded, self.deleted_files = [], []
        self.sessions, self.deleted_sessions = [], []
        self.messages = []            # (session_id, message) — must be one per session
        self.agents = []

    def upload_file(self, path):
        fid = f"file_{len(self.uploaded)}"
        self.uploaded.append(fid)
        return fid

    def delete_file(self, fid):
        self.deleted_files.append(fid)

    def list_files(self):
        if self.leftover:
            return list(self.uploaded)
        return [f for f in self.uploaded if f not in self.deleted_files]

    def create_agent(self, envelope):
        self.agents.append(envelope)
        return "agent_1"

    def create_environment(self, defaults=None):
        return "env_1"

    def create_session(self, agent_id, environment_id, file_ids, mount_path):
        sid = f"sess_{len(self.sessions)}"
        self.sessions.append(sid)
        return sid

    def send_message(self, session_id, message):
        self.messages.append((session_id, message))

    def get_events(self, session_id):
        if self.stall:
            return []
        if self.ask:
            return [{"role": "assistant", "text": "Where is the file? I can't find it."}]
        return [
            {"role": "assistant", "text": "Preflight: header row is "
             "order_id | customer | total; sample rows read."},
            {"role": "assistant", "text": "Appended exactly one row with the PO values."},
            {"role": "assistant", "text": "Confirmation: order received."},
        ]

    def download_outputs(self, session_id):
        if self.stall:
            return {}
        return {"SHEET.xlsx": "order_id,customer,total\nPO-1,Acme,30\n"}

    def delete_session(self, sid):
        self.deleted_sessions.append(sid)

    def list_sessions(self):
        if self.leftover:
            return list(self.sessions)
        return [s for s in self.sessions if s not in self.deleted_sessions]


def _pass_judge(c, transcript, outputs):
    if "external thread" in c:
        return {"verdict": "not_testable_in_sandbox", "evidence": "needs live email"}
    return {"verdict": "pass", "evidence": "transcript satisfies it"}


def _compiled_test():
    return compile_agent(BUNDLE, SPEC_MD, EVENT_LOG)["test"]["dict"]


class DeployerTest(unittest.TestCase):
    def test_happy_path_grades_and_cleans_up(self):
        client = FakeClient()
        report, artifacts = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG,
                                              _compiled_test(), client, judge_fn=_pass_judge)
        # files uploaded + mounted; session created; outputs downloaded
        self.assertEqual(len(client.uploaded), 2)
        self.assertIn("SHEET.xlsx", artifacts["output_files"])
        # zero coaching: exactly one message per session, ever
        self.assertEqual(len(client.messages), len(client.sessions))
        # retention: session + files deleted, nothing remains
        self.assertEqual(client.deleted_sessions, client.sessions)
        self.assertEqual(sorted(client.deleted_files), sorted(client.uploaded))
        self.assertTrue(report["retention_clean"])
        # graded against the spec's criteria; reproduces the pass result
        self.assertEqual(report["failed"], 0)
        self.assertTrue(report["deploy_recommended"])
        self.assertEqual(report["bundle_id"], "B1")
        self.assertGreaterEqual(report["passed"], 1)

    def test_failing_grade_does_not_recommend_deploy(self):
        client = FakeClient()
        report, _ = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(),
                                      client, judge_fn=lambda c, t, o: {"verdict": "fail", "evidence": "wrong"})
        self.assertGreater(report["failed"], 0)
        self.assertFalse(report["deploy_recommended"])

    def test_asked_question_recorded_not_papered_over(self):
        client = FakeClient(ask=True)
        report, _ = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(),
                                      client, judge_fn=_pass_judge)
        self.assertTrue(report["zero_coaching"]["asked_question"])
        # still only the trigger was sent — no steering
        self.assertEqual(len(client.messages), len(client.sessions))

    def test_stall_recorded_and_blocks_deploy(self):
        client = FakeClient(stall=True)
        report, _ = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(),
                                      client, judge_fn=_pass_judge)
        self.assertTrue(report["zero_coaching"]["stalled"])
        self.assertFalse(report["deploy_recommended"])

    def test_retention_failure_detected(self):
        client = FakeClient(leftover=True)   # platform "fails" to delete
        report, _ = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(),
                                      client, judge_fn=_pass_judge)
        self.assertFalse(report["retention_clean"])

    def test_multiple_instances_capped_one_message_each(self):
        env = {"artifacts": [{"role": "input", "uploaded": True, "file_ref": f"/s/in{i}.pdf",
                              "user_instruction": ""} for i in range(5)]}
        client = FakeClient()
        report, _ = deployer.run_test(BUNDLE, SPEC_MD, env, EVENT_LOG, _compiled_test(),
                                      client, judge_fn=_pass_judge, max_sessions=2)
        self.assertEqual(report["sessions_run"], 2)
        self.assertEqual(len(client.messages), 2)   # exactly one trigger per session


if __name__ == "__main__":
    unittest.main()
