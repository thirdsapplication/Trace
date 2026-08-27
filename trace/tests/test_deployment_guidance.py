"""A deployed agent must actually receive the lessons retrieval finds.

The bug, found live on 2026-08-27 while testing Voyage end to end:

    if action == "run-now":
        if agent.get("deployment_id"):
            state.run_deployment_now(agent)   # <- every real agent
        else:
            state.run_agent(agent)            # <- the ONLY caller of _retrieve_guidance

`_retrieve_guidance` was reachable only from `run_agent`, and 12 of 13 live agents are
deployment-backed. So lesson retrieval never executed in production: agent_embeddings,
lesson_promotions and lesson_applications sat at 0 and could not leave it by running an
agent. Nothing errored, and "0 rows" is indistinguishable from "nothing to share".

`run_deployment(deployment_id)` takes no per-run input, so guidance cannot simply be
passed in; the agent's memory store is the only channel a live deployment reads. These
tests pin both halves: that retrieval is CALLED on the deployment path, and that what it
returns is written where the agent will see it.
"""

import tempfile
import unittest

from trace.agent import config as agent_config
from trace.ui import app


class FakeClient:
    """Records what the platform was asked to do."""

    def __init__(self, write_result="create"):
        self.written = []
        self.runs = []
        self._write_result = write_result

    def write_memory(self, store_id, path, content):
        self.written.append({"store_id": store_id, "path": path, "content": content})
        return self._write_result

    def run_deployment(self, deployment_id):
        self.runs.append(deployment_id)
        return {"id": "drun_1", "session_id": "sesn_1"}


class DeploymentGuidanceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = app.AppState(sessions_root=self._tmp.name)
        self.client = FakeClient()
        self.state._live_client = lambda: self.client
        self.state._refresh_agent_binding = lambda a: None
        self.state._record_deployment_run = lambda *a, **k: None
        self.state._agent_dm = lambda *a, **k: None
        self.agent = {"agent_id": "a1", "owner_user_id": "u1", "name": "Shipping",
                      "deployment_id": "depl_1", "memory_store_id": "mem_1",
                      "vault_ref": "v1"}

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_deployment_path_asks_for_lessons_at_all(self):
        """The whole bug in one assertion."""
        asked = []
        self.state._retrieve_guidance = lambda a: asked.append(a.get("agent_id")) or []
        self.state.run_deployment_now(self.agent)
        self.assertEqual(asked, ["a1"],
                         "run_deployment_now never retrieved lessons — every real agent "
                         "takes this path, so cross-user learning cannot happen")

    def test_retrieved_lessons_are_written_to_the_shared_document(self):
        self.state._retrieve_guidance = lambda a: ["Use the shipped date when order date is blank",
                                                   "Skip rows with no tracking number"]
        self.state.run_deployment_now(self.agent)
        self.assertEqual(len(self.client.written), 1)
        doc = self.client.written[0]
        self.assertEqual(doc["store_id"], "mem_1")
        self.assertEqual(doc["path"], agent_config.MEMORY_SHARED_DOC)
        self.assertIn("Use the shipped date when order date is blank", doc["content"])
        self.assertIn("Skip rows with no tracking number", doc["content"])

    def test_it_is_not_written_into_the_agents_own_lessons_file(self):
        """lessons_learned.md is rewritten by the agent every session; borrowed lessons
        put there would be clobbered, and would also look like its own conclusions."""
        self.state._retrieve_guidance = lambda a: ["a lesson"]
        self.state.run_deployment_now(self.agent)
        paths = [w["path"] for w in self.client.written]
        self.assertNotIn(agent_config.MEMORY_LESSONS_DOC, paths)
        self.assertNotIn(agent_config.MEMORY_STATUS_DOC, paths)

    def test_delivery_happens_before_the_run_is_triggered(self):
        """The run reads memory at session start, so a late write is a wasted write."""
        order = []
        self.state._retrieve_guidance = lambda a: ["x"]
        self.client.write_memory = lambda *a, **k: order.append("write") or "create"
        self.client.run_deployment = lambda d: order.append("run") or {"id": "d", "session_id": "s"}
        self.state.run_deployment_now(self.agent)
        self.assertEqual(order, ["write", "run"])

    def test_no_lessons_clears_stale_advice_rather_than_leaving_it(self):
        self.state._retrieve_guidance = lambda a: []
        self.state.run_deployment_now(self.agent)
        self.assertEqual(len(self.client.written), 1)
        self.assertIn("(none yet)", self.client.written[0]["content"])

    def test_an_agent_with_no_memory_store_still_runs(self):
        """Older agents predate the shared document. They must not stop working."""
        self.state._retrieve_guidance = lambda a: ["a lesson"]
        agent = dict(self.agent, memory_store_id=None)
        self.state.run_deployment_now(agent)
        self.assertEqual(self.client.written, [])
        self.assertEqual(self.client.runs, ["depl_1"])

    def test_a_retrieval_failure_never_blocks_the_run(self):
        def boom(_a):
            raise RuntimeError("voyage is down")
        self.state._retrieve_guidance = boom
        self.state.run_deployment_now(self.agent)
        self.assertEqual(self.client.runs, ["depl_1"],
                         "a learning problem must never cost the user their run")

    def test_a_memory_write_failure_never_blocks_the_run(self):
        self.state._retrieve_guidance = lambda a: ["x"]

        def boom(*_a, **_k):
            raise RuntimeError("store unreachable")
        self.client.write_memory = boom
        self.state.run_deployment_now(self.agent)
        self.assertEqual(self.client.runs, ["depl_1"])


class SharedDocumentTest(unittest.TestCase):
    """The document only works if the agent is told it exists."""

    def test_the_shared_doc_is_seeded_on_every_store(self):
        """Assert on the REAL default list, not one the test builds for itself.

        The first version of this test constructed the three-document list inline and
        then asserted the third was in it — a guard that could not fail. It has to read
        create_or_reuse_memory_store's own default.
        """
        import inspect

        from trace.agent import api_client
        src = inspect.getsource(api_client.ManagedAgentsClient.create_or_reuse_memory_store)
        head = src.split("stores = self._agents_ns")[0]     # the `documents = ...` default
        self.assertIn("MEMORY_SHARED_DOC", head,
                      "the shared-lessons document is not in the default seed list, so "
                      "stores are created without it and nothing can be delivered")
        self.assertIn("MEMORY_STATUS_DOC", head)            # the others must survive too
        self.assertIn("MEMORY_LESSONS_DOC", head)

    def test_the_run_directive_tells_the_agent_to_read_it(self):
        """Without this the file is written and never opened."""
        self.assertIn("shared_lessons.md", agent_config.MEMORY_UPDATE_DIRECTIVE)
        self.assertIn("shared_lessons.md", agent_config.MEMORY_STORE_DESCRIPTION)

    def test_the_agent_is_told_not_to_edit_it(self):
        self.assertIn("never edit that file", agent_config.MEMORY_UPDATE_DIRECTIVE)


if __name__ == "__main__":
    unittest.main()
