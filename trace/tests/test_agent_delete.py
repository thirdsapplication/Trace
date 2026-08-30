"""Deleting an agent, and deleting a recording that has no agent.

Two promises the product made and could not keep, found 2026-08-30 while verifying the
deletion path against live data:

1. The onboarding copy says, twice, that you can delete any agent at any time. No
   endpoint, no registry function and no button existed — only delete-capture-data,
   which leaves the agent live with its vault and schedule intact.

2. Both capture-delete entry points resolve through an AGENT, so 51 of the 66 sessions
   holding cloud recordings had no delete path at all.

The sharp edge in (1) is the memory store: two of the founder's own agents shared one,
so deleting a store along with its agent would have wiped a surviving agent's memory.
"""

import tempfile
import unittest

from trace.ui import app, registry, sessions


class FakeCleanup:
    def __init__(self, boom=False):
        self.calls = []
        self.boom = boom

    def __call__(self, session_ids, file_ids, agent_ids, agent_names=None,
                 deployment_ids=None, memory_store_ids=None):
        if self.boom:
            raise RuntimeError("platform unreachable")
        self.calls.append({"deployment_ids": deployment_ids or [],
                           "memory_store_ids": memory_store_ids or []})
        return {"ok": True}


class AgentDeleteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.cleanup = FakeCleanup()
        self.state = app.AppState(sessions_root=self.root, cleanup_fn=self.cleanup)
        registry.agent_upsert(self.root, {
            "agent_id": "a1", "owner_user_id": "u1", "name": "Shipping",
            "bundle_id": "B1", "session_id": "s1", "deployment_id": "depl_1",
            "memory_store_id": "mem_shared", "vault_ref": "v1"})
        registry.agent_upsert(self.root, {
            "agent_id": "a2", "owner_user_id": "u1", "name": "Remittance",
            "bundle_id": "B2", "session_id": "s2", "deployment_id": "depl_2",
            "memory_store_id": "mem_shared", "vault_ref": "v1"})
        registry.agent_upsert(self.root, {
            "agent_id": "a3", "owner_user_id": "u2", "name": "Someone else's",
            "bundle_id": "B3", "session_id": "s3"})

    def tearDown(self):
        self._tmp.cleanup()

    def _ids(self, owner="u1"):
        return sorted(a["agent_id"] for a in registry.agents_for_user(self.root, owner))

    def test_the_agent_is_actually_gone(self):
        res = self.state.delete_agent("a1", "u1")
        self.assertTrue(res["deleted"])
        self.assertEqual(self._ids(), ["a2"])

    def test_the_deployment_is_archived(self):
        """The failure that costs something is a live deployment still running against
        a vault after its owner believes it is gone."""
        self.state.delete_agent("a1", "u1")
        self.assertEqual(self.cleanup.calls[0]["deployment_ids"], ["depl_1"])

    def test_a_shared_memory_store_is_NOT_deleted(self):
        """a1 and a2 share mem_shared. Deleting it with a1 wipes a2's memory."""
        self.state.delete_agent("a1", "u1")
        self.assertEqual(self.cleanup.calls[0]["memory_store_ids"], [],
                         "deleted a memory store another live agent still uses")
        self.assertEqual(self.state.delete_agent("a2", "u1")["memory_store_kept_for"], [])

    def test_the_last_agent_using_a_store_does_delete_it(self):
        self.state.delete_agent("a1", "u1")
        self.state.delete_agent("a2", "u1")
        self.assertEqual(self.cleanup.calls[1]["memory_store_ids"], ["mem_shared"])

    def test_you_cannot_delete_another_users_agent(self):
        res = self.state.delete_agent("a3", "u1")
        self.assertEqual(res, {"error": "not_found"})
        self.assertEqual([a["agent_id"] for a in registry.agents_for_user(self.root, "u2")],
                         ["a3"])

    def test_a_platform_failure_is_reported_not_swallowed(self):
        """Rows gone + deployment alive is the state that must never look clean."""
        state = app.AppState(sessions_root=self.root, cleanup_fn=FakeCleanup(boom=True))
        res = state.delete_agent("a1", "u1")
        self.assertTrue(res["deleted"])
        self.assertTrue(res["errors"], "a failed platform cleanup was reported as success")
        self.assertIn("orphaned", res["errors"][0])

    def test_children_go_with_it(self):
        registry.message_add(self.root, {"agent_id": "a1", "owner_user_id": "u1",
                                         "sender": "agent", "text": "hi"})
        registry.schedule_set(self.root, "a1", "u1", {"kind": "daily", "time": "09:00"})
        self.state.delete_agent("a1", "u1")
        self.assertIsNone(registry.schedule_get(self.root, "a1"))
        self.assertEqual(registry.messages_for_agent(self.root, "u1", "a1"), [])

    def test_the_other_agents_schedule_survives(self):
        registry.schedule_set(self.root, "a2", "u1", {"kind": "daily", "time": "09:00"})
        self.state.delete_agent("a1", "u1")
        self.assertIsNotNone(registry.schedule_get(self.root, "a2"),
                             "deleting one agent took another's schedule with it")


class SessionCaptureDeleteTest(unittest.TestCase):
    """A recording with no agent must still be deletable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(sessions_root=self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_owner_is_resolved_without_a_meta_file(self):
        """read_meta returns {"id": sid} on a wiped disk, so the old gate compared None
        to the owner and answered not_found — on Render, where there is no persistent
        disk, that was every session."""
        sid = sessions.create_session(self.root, owner_id="u1")
        self.assertEqual(sessions.owner_of(self.root, sid), "u1")

    def test_a_stranger_cannot_delete_it(self):
        sid = sessions.create_session(self.root, owner_id="u1")
        self.assertEqual(self.state.delete_capture_data(sid, "u2"), {"error": "not_found"})

    def test_the_owner_can(self):
        sid = sessions.create_session(self.root, owner_id="u1")
        res = self.state.delete_capture_data(sid, "u1")
        self.assertTrue(res.get("deleted"))


if __name__ == "__main__":
    unittest.main()
