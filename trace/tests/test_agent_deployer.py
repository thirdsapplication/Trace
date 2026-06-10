"""Stage 4 — Test Deployer (offline, stub API client + stub judge).

Verifies the full sandbox orchestration without a real platform: files uploaded +
mounted, one session per instance, ONLY the trigger sent (zero coaching), outputs
downloaded, session + files deleted (retention clean), and grading against the
spec's own criteria — including the gate's ability to fail.
"""

import json
import os
import tempfile
import unittest

from trace.agent import api_client, deployer
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

    def create_session(self, agent_id, environment_id, resources, title=None):
        sid = f"sess_{len(self.sessions)}"
        self.sessions.append(sid)
        self.last_resources = resources
        return sid

    def send_trigger_and_collect(self, session_id, message):
        self.messages.append((session_id, message))      # the only message, ever
        if self.stall:
            return [{"type": "session.status_idle", "text": "", "name": None}]
        if self.ask:
            return [{"type": "agent.message", "text": "Where is the file? I can't find it.", "name": None},
                    {"type": "session.status_idle", "text": "", "name": None}]
        return [
            {"type": "agent.message", "text": "Preflight: header row is "
             "order_id | customer | total; sample rows read.", "name": None},
            {"type": "agent.tool_use", "text": "", "name": "write_file"},
            {"type": "agent.message", "text": "Appended exactly one row with the PO values.", "name": None},
            {"type": "agent.message", "text": "Confirmation: order received.", "name": None},
            {"type": "session.status_idle", "text": "", "name": None},
        ]

    def download_outputs(self, session_id, exclude_ids=None):
        self.last_exclude_ids = exclude_ids
        if self.stall:
            return {}, []
        return {"SHEET.xlsx": "order_id,customer,total\nPO-1,Acme,30\n"}, []

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

    def test_session_error_is_non_fatal_and_still_cleans_up(self):
        # a hung/erroring session (e.g. the platform never sends a terminal event)
        # is recorded, blocks deploy, and is still deleted — nothing leaks.
        client = FakeClient()
        def boom(session_id, message):
            client.messages.append((session_id, message))
            raise RuntimeError("stream hung")
        client.send_trigger_and_collect = boom
        report, _ = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(),
                                      client, judge_fn=_pass_judge)
        self.assertEqual(client.deleted_sessions, client.sessions)   # cleaned up anyway
        self.assertEqual(sorted(client.deleted_files), sorted(client.uploaded))
        self.assertFalse(report["deploy_recommended"])               # stalled -> no deploy

    def test_download_failure_after_stream_preserves_events_on_disk(self):
        # (a) the stream is captured, then the download fails ("file is not
        # downloadable"): the events must already be on disk, the error appended (not
        # substituted), and the grade computed from the REAL stream.
        client = FakeClient()
        def boom_dl(session_id, exclude_ids=None):
            raise RuntimeError("file is not downloadable")
        client.download_outputs = boom_dl
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "events_B1.json")
            report, _ = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG,
                                          _compiled_test(), client, judge_fn=_pass_judge,
                                          events_path=ep)
            self.assertTrue(os.path.exists(ep))          # persisted before the crash
            with open(ep, encoding="utf-8") as f:
                saved = json.load(f)
        inst = saved["instances"][0]
        types = [e["type"] for e in inst["events"]]
        self.assertIn("agent.message", types)            # the real stream survived...
        self.assertIn("client.download_error", types)    # ...and the error was appended
        # grade reflects the real stream: the agent produced text, criteria pass
        self.assertGreaterEqual(report["passed"], 1)
        self.assertFalse(report["zero_coaching"]["stalled"])

    def test_download_excludes_our_uploaded_inputs(self):
        # (b) the session-scoped listing mixes mounted-input copies (which the platform
        # refuses to download) with agent-created files; only agent files under the
        # outputs path are downloaded.
        objs = [
            {"id": "file_in",    "filename": "PO.pdf",     "path": "/workspace/files/PO.pdf"},
            {"id": "file_out",   "filename": "result.csv", "path": "/workspace/outputs/result.csv"},
            {"id": "file_nd",    "filename": "blocked.csv", "downloadable": False,
             "path": "/workspace/outputs/blocked.csv"},
            {"id": "file_other", "filename": "scratch.txt", "path": "/tmp/scratch.txt"},
        ]
        selected = api_client._select_downloadable(objs, {"file_in"}, "/workspace/outputs")
        # mounted input excluded by id, non-downloadable dropped, scratch dropped in
        # favor of the outputs-path file
        self.assertEqual(selected, [("file_out", "result.csv")])

    def test_download_per_file_fault_tolerance(self):
        # (c) one un-downloadable file among several yields a per-file warning; the
        # other files still download and the run completes.
        client = api_client.ManagedAgentsClient.__new__(api_client.ManagedAgentsClient)
        client._beta_version = "test"

        class _Body:
            def __init__(self, fid): self.text = f"content-{fid}"

        class FakeFiles:
            def list(self, scope_id=None, betas=None):
                return [{"id": "a", "filename": "good1.csv", "path": "/workspace/outputs/good1.csv"},
                        {"id": "b", "filename": "bad.csv",   "path": "/workspace/outputs/bad.csv"},
                        {"id": "c", "filename": "good2.csv", "path": "/workspace/outputs/good2.csv"}]

            def download(self, fid):
                if fid == "b":
                    raise RuntimeError("file is not downloadable")
                return _Body(fid)

        client._files = lambda: FakeFiles()
        out, warnings = client.download_outputs("sess", exclude_ids=[])
        self.assertEqual(set(out), {"good1.csv", "good2.csv"})
        self.assertEqual(out["good1.csv"], "content-a")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["file"], "bad.csv")

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
