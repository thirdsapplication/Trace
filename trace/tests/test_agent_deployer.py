"""Stage 4 — Test Deployer in MANUAL-EVALUATION mode (offline, stub API client).

Verifies the run orchestration without a real platform: files uploaded + mounted,
one session per instance, ONLY the trigger sent (zero coaching), the agent
created-or-reused (never accumulated), output filenames listed (not downloaded),
the event stream persisted locally — and crucially **no grading and no deletion**
in the flow (the session stays in the Console for manual review; cleanup is a
separate, explicit action).
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
    """Records calls; simulates a successful single-instance run. Tracks deletions so
    tests can assert NONE happen automatically, and reuses agents by name."""

    def __init__(self, stall=False, raise_stream=False):
        self.stall, self.raise_stream = stall, raise_stream
        self.uploaded, self.deleted_files = [], []
        self.sessions, self.deleted_sessions = [], []
        self.messages = []            # (session_id, message) — must be one per session
        self.agents = []              # [{id, name}] — created agents (reuse by name)

    def upload_file(self, path):
        fid = f"file_{len(self.uploaded)}"
        self.uploaded.append(fid)
        return fid

    def create_or_reuse_agent(self, envelope):
        name = envelope["name"]
        for a in self.agents:
            if a["name"] == name:
                return {"id": a["id"], "name": name, "reused": True}
        aid = f"agent_{len(self.agents)}"
        self.agents.append({"id": aid, "name": name})
        return {"id": aid, "name": name, "reused": False}

    def create_environment(self, defaults=None):
        return "env_1"

    def create_session(self, agent_id, environment_id, resources, title=None):
        sid = f"sess_{len(self.sessions)}"
        self.sessions.append(sid)
        self.last_resources = resources
        return sid

    def send_trigger_and_collect(self, session_id, message):
        self.messages.append((session_id, message))      # the only message, ever
        if self.raise_stream:
            raise RuntimeError("stream hung")
        if self.stall:
            return [{"type": "session.status_idle", "text": "", "name": None}]
        return [
            {"type": "agent.message", "text": "Preflight: header row read; sample rows read.", "name": None},
            {"type": "agent.tool_use", "text": "", "name": "write_file"},
            {"type": "agent.message", "text": "Appended exactly one row with the PO values.", "name": None},
            {"type": "session.status_idle", "text": "", "name": None},
        ]

    def list_output_files(self, session_id, exclude_ids=None):
        self.last_exclude_ids = exclude_ids
        return [] if self.stall else ["result.csv"]

    # deletion methods exist but must NOT be called by the run flow
    def delete_session(self, sid):
        self.deleted_sessions.append(sid)

    def delete_file(self, fid):
        self.deleted_files.append(fid)


def _compiled_test():
    return compile_agent(BUNDLE, SPEC_MD, EVENT_LOG)["test"]["dict"]


class DeployerTest(unittest.TestCase):
    def test_happy_path_returns_pointer_and_does_not_delete(self):
        client = FakeClient()
        run = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(), client)
        # files uploaded + mounted; agent created; one session
        self.assertEqual(len(client.uploaded), 2)
        self.assertEqual(run["bundle_id"], "B1")
        self.assertTrue(run["agent_id"])
        self.assertEqual(len(run["sessions"]), 1)
        s = run["sessions"][0]
        # the run result is a POINTER: session id, console link, status, output names
        self.assertEqual(s["session_id"], "sess_0")
        self.assertTrue(s["console_url"])
        self.assertEqual(s["status"], "completed")
        self.assertEqual(s["output_files"], ["result.csv"])
        # zero coaching: exactly one message per session, ever
        self.assertEqual(len(client.messages), len(client.sessions))
        # NOTHING is deleted automatically — the session stays in the Console
        self.assertEqual(client.deleted_sessions, [])
        self.assertEqual(client.deleted_files, [])
        # mounted inputs are excluded from the output listing
        self.assertEqual(sorted(client.last_exclude_ids), sorted(client.uploaded))

    def test_no_grading_keys_in_run_record(self):
        client = FakeClient()
        run = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(), client)
        for k in ("passed", "failed", "deploy_recommended", "results", "zero_coaching"):
            self.assertNotIn(k, run)

    def test_agent_reused_across_rounds(self):
        client = FakeClient()
        r1 = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(), client)
        r2 = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(), client)
        self.assertFalse(r1["agent_reused"])
        self.assertTrue(r2["agent_reused"])                  # same name -> reused
        self.assertEqual(r1["agent_id"], r2["agent_id"])
        self.assertEqual(len(client.agents), 1)              # no accumulation

    def test_errored_session_recorded_and_not_deleted(self):
        client = FakeClient(raise_stream=True)
        run = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(), client)
        s = run["sessions"][0]
        self.assertEqual(s["status"], "errored")
        self.assertIn("stream hung", s["error"])
        self.assertEqual(client.deleted_sessions, [])        # still never auto-deleted

    def test_event_stream_persisted_to_disk(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "events_B1.json")
            run = deployer.run_test(BUNDLE, SPEC_MD, ENV_RECORD, EVENT_LOG, _compiled_test(),
                                    client, events_path=ep)
            self.assertTrue(os.path.exists(ep))
            with open(ep, encoding="utf-8") as f:
                saved = json.load(f)
        types = [e["type"] for e in saved["instances"][0]["events"]]
        self.assertIn("agent.message", types)                # the real stream is the backup record
        self.assertEqual(run["events_file"], "events_B1.json")

    def test_one_session_per_bundle_by_default(self):
        # default max_sessions is 1 (manual mode, credit-conscious) even with many inputs
        env = {"artifacts": [{"role": "input", "uploaded": True, "file_ref": f"/s/in{i}.pdf",
                              "user_instruction": ""} for i in range(5)]}
        client = FakeClient()
        run = deployer.run_test(BUNDLE, SPEC_MD, env, EVENT_LOG, _compiled_test(), client)
        self.assertEqual(len(run["sessions"]), 1)
        self.assertEqual(len(client.messages), 1)

    # --- download helpers stay in the client (used to LIST outputs; return later) ---
    def test_select_downloadable_excludes_inputs_and_prefers_outputs(self):
        objs = [
            {"id": "file_in",    "filename": "PO.pdf",     "path": "/workspace/files/PO.pdf"},
            {"id": "file_out",   "filename": "result.csv", "path": "/workspace/outputs/result.csv"},
            {"id": "file_nd",    "filename": "blocked.csv", "downloadable": False,
             "path": "/workspace/outputs/blocked.csv"},
            {"id": "file_other", "filename": "scratch.txt", "path": "/tmp/scratch.txt"},
        ]
        selected = api_client._select_downloadable(objs, {"file_in"}, "/workspace/outputs")
        self.assertEqual(selected, [("file_out", "result.csv")])

    def test_download_per_file_fault_tolerance(self):
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
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["file"], "bad.csv")

    def test_cleanup_deletes_only_given_ids_and_reports(self):
        client = api_client.ManagedAgentsClient.__new__(api_client.ManagedAgentsClient)
        client._beta_version = "test"
        deleted = {"sessions": [], "agents": [], "files": []}

        class Ns:
            def __init__(self, kind): self.kind = kind
            def delete(self, _id, **kw): deleted[self.kind].append(_id)

        class FakeFiles:
            def delete(self, _id, **kw): deleted["files"].append(_id)

        client._agents_ns = lambda name: Ns(name)
        client._files = lambda: FakeFiles()
        res = client.cleanup(session_ids=["s1", "s2"], file_ids=["f1"], agent_ids=["a1"])
        self.assertEqual(deleted["sessions"], ["s1", "s2"])
        self.assertEqual(deleted["files"], ["f1"])
        self.assertEqual(deleted["agents"], ["a1"])
        self.assertEqual(res["removed"]["sessions"], ["s1", "s2"])
        self.assertEqual(res["errors"], [])

    def test_cleanup_sweeps_orphans_by_name(self):
        # stale/duplicate agents under this session's workflow names are swept too,
        # even when they aren't in the recorded ids.
        client = api_client.ManagedAgentsClient.__new__(api_client.ManagedAgentsClient)
        client._beta_version = "test"
        deleted = []

        class AgentsNs:
            def list(self):
                return [{"id": "a_old", "name": "Order intake"},     # stale duplicate
                        {"id": "a_keep", "name": "Someone else's agent"}]
            def delete(self, _id, **kw):
                deleted.append(_id)

        client._agents_ns = lambda name: AgentsNs()
        res = client.cleanup(agent_ids=["a_live"], agent_names=["Order intake"])
        self.assertIn("a_live", deleted)                 # the recorded id
        self.assertIn("a_old", deleted)                  # the swept orphan
        self.assertNotIn("a_keep", deleted)              # an unrelated agent is untouched
        self.assertEqual(set(res["removed"]["agents"]), {"a_live", "a_old"})


if __name__ == "__main__":
    unittest.main()
