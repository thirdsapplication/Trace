"""Spec 4 — Trace-side scheduler: the headless run path (runner), cadence
due-logic, a scheduler pass that runs a due agent and records it, schedule
durability across a restart, and the desktop-node guard. Offline against a stub
MA client + file-mode registry (no DATABASE_URL).
"""

import tempfile
import time
import unittest

from trace.ui import app, registry
from trace.agent import runner
from trace.tests.test_ui import FakeKeystore


class FakeMA:
    """Minimal Managed Agents client for the run path."""
    def __init__(self, events=None, held=None, fail=False):
        self.events = events if events is not None else [{"type": "session.status_idle", "text": "Handled 2 emails"}]
        self.held = held or []
        self.fail = fail
        self.sessions = []

    def create_environment(self):
        return "env-1"

    def create_session(self, agent_id, env, resources, title=None, vault_ids=None):
        if self.fail:
            raise RuntimeError("platform down")
        self.sessions.append({"agent_id": agent_id, "vault_ids": vault_ids})
        return "run-sess-1"

    def send_trigger_and_collect(self, sid, trigger, timeout=None, max_events=None):
        return self.events

    def list_pending_approvals(self, sid):
        return self.held


def _tool(name):
    return {"type": "agent.mcp_tool_use", "name": name, "text": ""}


def _agent(owner="usr_1", agent_id="agent-1"):
    return {"agent_id": agent_id, "owner_user_id": owner, "external_user_id": "x",
            "bundle_id": "wf", "name": "WF", "vault_ref": "vlt-1",
            "trigger": "on a new task", "session_id": "s"}


class RunnerTest(unittest.TestCase):
    def test_completed_run(self):
        rec = runner.run_once(FakeMA(), _agent())
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["summary"], "Handled 2 emails")
        self.assertEqual(rec["run_session_id"], "run-sess-1")

    def test_always_allow_actions_are_completed_not_pending(self):
        # Spec 6 bug: an always-allow agent's executed tool calls (mcp_tool_use with
        # NO requires_action stop, even if the platform over-reports them as held)
        # must read as completed actions — never "needs approval".
        events = [_tool("update_row"), _tool("update_row"), _tool("update_row"),
                  {"type": "session.status_idle", "stop_reason": "end_turn", "text": ""}]
        ma = FakeMA(events=events, held=[{"action_id": "x", "summary": "y"}] * 3)  # over-reported
        rec = runner.run_once(ma, _agent())
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(len(rec["details"]), 3)               # 3 actions TAKEN
        self.assertIn("Completed 3 actions", rec["summary"])
        self.assertNotIn("approv", rec["summary"].lower())

    def test_needs_approval_only_on_requires_action_stop(self):
        events = [_tool("send_email"),
                  {"type": "session.status_idle", "stop_reason": "requires_action"}]
        ma = FakeMA(events=events, held=[{"action_id": "a1", "summary": "send email to Bob"}])
        rec = runner.run_once(ma, _agent())
        self.assertEqual(rec["status"], "needs_approval")
        self.assertIn("approve", rec["summary"].lower())
        self.assertEqual(rec["details"], ["send email to Bob"])

    def test_failed_event(self):
        rec = runner.run_once(FakeMA(events=[{"type": "session.failed", "text": "nope"}]), _agent())
        self.assertEqual(rec["status"], "failed")

    def test_platform_error_is_failed_not_raised(self):
        rec = runner.run_once(FakeMA(fail=True), _agent())
        self.assertEqual(rec["status"], "failed")
        self.assertIsNone(rec["run_session_id"])

    def test_no_run_record_references_console(self):
        rec = runner.run_once(FakeMA(), _agent())
        self.assertNotIn("console_url", rec)
        self.assertNotIn("console", repr(rec).lower())

    def test_vault_injected(self):
        ma = FakeMA()
        runner.run_once(ma, _agent())
        self.assertEqual(ma.sessions[0]["vault_ids"], ["vlt-1"])


class ScheduleDueTest(unittest.TestCase):
    def test_interval(self):
        c = {"kind": "interval", "minutes": 15}
        self.assertTrue(runner.schedule_due(c, None, 1000))           # never run -> due
        self.assertFalse(runner.schedule_due(c, 1000, 1000 + 600))    # 10 min later -> not due
        self.assertTrue(runner.schedule_due(c, 1000, 1000 + 900))     # 15 min later -> due

    def test_hourly(self):
        c = {"kind": "hourly"}
        self.assertTrue(runner.schedule_due(c, 1000, 1000 + 3600))
        self.assertFalse(runner.schedule_due(c, 1000, 1000 + 1800))

    def test_none_never_due(self):
        self.assertFalse(runner.schedule_due(None, None, 1000))


class SchedulerPassTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(self.root, key_store=FakeKeystore())
        self.ma = FakeMA()
        self.state._live_client = lambda: self.ma                     # inject stub client

    def tearDown(self):
        self.state.scheduler_stop.set()
        self._tmp.cleanup()

    def test_due_agent_runs_and_is_recorded(self):
        registry.agent_upsert(self.root, _agent())
        registry.schedule_set(self.root, "agent-1", "usr_1", {"kind": "interval", "minutes": 15})
        self.state.run_due_schedules(now=time.time())
        runs = registry.runs_for_agent(self.root, "agent-1")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        # immediately again -> not due (just ran), still one run
        self.state.run_due_schedules(now=time.time())
        self.assertEqual(len(registry.runs_for_agent(self.root, "agent-1")), 1)

    def test_paused_schedule_does_not_run(self):
        registry.agent_upsert(self.root, _agent())
        registry.schedule_set(self.root, "agent-1", "usr_1", {"kind": "hourly"}, paused=True)
        self.state.run_due_schedules(now=time.time())
        self.assertEqual(registry.runs_for_agent(self.root, "agent-1"), [])

    def test_schedule_persists_across_restart(self):
        registry.agent_upsert(self.root, _agent())
        registry.schedule_set(self.root, "agent-1", "usr_1", {"kind": "hourly"})
        # "restart": a brand-new AppState on the same root still sees the schedule.
        fresh = app.AppState(self.root, key_store=FakeKeystore())
        active = registry.schedule_list_active(fresh.sessions_root)
        self.assertEqual([a["agent_id"] for a in active], ["agent-1"])

    def test_scheduler_does_not_start_on_desktop_node(self):
        desktop = app.AppState(self.root, key_store=FakeKeystore(),
                               cloud_backend="https://cloud.example")
        desktop.start_scheduler()
        self.assertIsNone(desktop._scheduler_thread)                  # proxies; never schedules
        local = app.AppState(self.root, key_store=FakeKeystore())
        local.start_scheduler()
        self.assertIsNotNone(local._scheduler_thread)
        local.scheduler_stop.set()


if __name__ == "__main__":
    unittest.main()
