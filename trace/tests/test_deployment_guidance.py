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

    def test_it_is_not_written_into_the_agents_own_status_file(self):
        """The status file is rewritten by the agent every session; lessons put there
        would be clobbered, and would also read as its own conclusions."""
        self.state._retrieve_guidance = lambda a: ["a lesson"]
        self.state.run_deployment_now(self.agent)
        paths = [w["path"] for w in self.client.written]
        self.assertEqual(paths, [agent_config.MEMORY_SHARED_DOC])

    def test_rejections_ride_the_same_door(self):
        """A rejected proposal used to be baked into a freshly-created deployment.

        Deployments are immutable, so telling an agent "don't propose that again" meant
        destroying and rebuilding a live deployment to change one paragraph. The memory
        store is rewritten before every run and costs nothing to change.
        """
        self.state._retrieve_guidance = lambda a: []
        self.state._rejected_for = lambda a, limit=20: [
            {"summary": "Email Ada about PO-9", "app": "gmail",
             "why_rejected": "the ship date isn't confirmed yet"}]
        self.state.run_deployment_now(self.agent)
        content = self.client.written[0]["content"]
        self.assertIn("Email Ada about PO-9", content)
        self.assertIn("the ship date isn't confirmed yet", content)
        self.assertEqual(self.client.written[0]["path"], agent_config.MEMORY_SHARED_DOC)

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
        # ONLY live state is seeded now. A store's shape past that is the
        # self-improvement engine's to discover per workflow, so shipping a lessons file
        # with every agent was Trace deciding it in advance for all of them.
        self.assertIn("MEMORY_STATUS_DOC", head)
        self.assertNotIn("MEMORY_LESSONS_DOC", head)
        # The shared document is not seeded either, and does not need to be: it is
        # written on demand, and write_memory creates a path that does not exist. A
        # fresh store therefore holds live state alone and delivery still works.
        self.assertNotIn("MEMORY_SHARED_DOC", head)

    def test_the_run_directive_tells_the_agent_to_read_it(self):
        """Without this the file is written and never opened.

        The rule is general rather than a clause naming this file: the engine can create
        documents nobody anticipated, so an instruction that lists filenames goes stale
        the first time it does. "List the directory and read every file" covers a document
        Trace writes and one the engine invented equally."""
        d = agent_config.MEMORY_UPDATE_DIRECTIVE
        self.assertIn("LIST that directory", d)
        self.assertIn("read EVERY file in it", d)
        self.assertIn("read every file in it", agent_config.MEMORY_STORE_DESCRIPTION)

    def test_the_agent_is_told_not_to_edit_it(self):
        """The rule is general now, not one clause about shared_lessons.md.

        The learning engine can create documents the agent has never seen, so naming
        the read-only files one at a time cannot keep up. Everything except the status
        ledger is written FOR the agent.
        """
        d = agent_config.MEMORY_UPDATE_DIRECTIVE
        self.assertIn("current_workflow_status.md is the ONLY file you write", d)
        self.assertIn("do not edit them", d)
        self.assertIn("do not create new ones", d)

    def test_the_agent_is_not_asked_to_write_its_own_lessons(self):
        """Distilling a session into lessons belongs to the self-improvement engine.

        An agent summarising the session it just lived is the worst-placed judge of what
        went wrong in it, and what it writes becomes confident, unreviewed advice to its
        own future self. It keeps a progress ledger; Trace keeps the lessons.
        """
        d = agent_config.MEMORY_UPDATE_DIRECTIVE
        self.assertNotIn("APPEND to", d)
        self.assertIn("do not write up lessons or retrospectives", d)
        self.assertIn("current_workflow_status.md is the ONLY file you write", d)
        # The directive no longer NAMES a lessons file, because there may not be one:
        # whether this agent has one, and what it is called, is the engine's decision.
        # The prohibition is on writing anything but the ledger, which covers a file
        # that does not exist yet as cleanly as one that does.
        self.assertNotIn("lessons_learned.md", d)

    def test_the_agent_still_tracks_its_progress(self):
        """The half that stays: the cumulative ledger is what stops re-doing work."""
        d = agent_config.MEMORY_UPDATE_DIRECTIVE
        self.assertIn("REWRITE current_workflow_status.md", d)
        self.assertIn("Already handled", d)


class StoreSeedingTest(unittest.TestCase):
    """A store now starts with live state and nothing else.

    shared_lessons.md is written by _deliver_guidance when there is something to say, and
    lessons_learned.md exists only if the self-improvement engine decides this agent wants
    one. Seeding either would be Trace deciding in advance the shape of a memory the
    engine is meant to discover per workflow."""

    def test_only_the_status_document_is_seeded(self):
        seeded = []

        class FakeMemories:
            def list(self, *a, **k):
                return []

            def create(self, store_id, path=None, content=None):
                seeded.append(path)
                return {"id": "mem_1"}

        class FakeStores:
            memories = FakeMemories()

            def list(self, *a, **k):
                return []

            def create(self, **kw):
                return {"id": "memstore_1"}

        from trace.tests.test_agent_api_client import _NSClient
        client = _NSClient.build({"memory_stores": FakeStores()})
        client.create_or_reuse_memory_store("store-name", "desc")
        self.assertEqual(seeded, [agent_config.MEMORY_STATUS_DOC])


class MemoryOwnershipTest(unittest.TestCase):
    """Who may write what. Two documents have a writer that is not the engine, and both
    are unreachable to it in code rather than merely discouraged in a prompt."""

    def test_the_engine_cannot_write_either_trace_or_agent_owned_document(self):
        from trace.agent import learn
        self.assertIsNone(learn._doc_path(agent_config.MEMORY_STATUS_DOC))
        self.assertIsNone(learn._doc_path(agent_config.MEMORY_SHARED_DOC))

    def test_the_engine_can_write_anything_else(self):
        from trace.agent import learn
        self.assertEqual(learn._doc_path("/whatever_it_invents.md"),
                         "/whatever_it_invents.md")

    def test_the_agent_is_told_status_is_the_only_file_it_writes(self):
        self.assertIn("ONLY file you write", agent_config.MEMORY_UPDATE_DIRECTIVE)

    def test_the_agent_is_no_longer_told_to_append_lessons(self):
        self.assertNotIn("lessons_learned", agent_config.MEMORY_UPDATE_DIRECTIVE)


if __name__ == "__main__":
    unittest.main()
