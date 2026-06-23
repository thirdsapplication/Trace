"""Spec 4 — Trace-side scheduler: the headless run path (runner), cadence
due-logic, a scheduler pass that runs a due agent and records it, schedule
durability across a restart, and the desktop-node guard. Offline against a stub
MA client + file-mode registry (no DATABASE_URL).
"""

import datetime
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
        self.approved, self.rejected, self.triggers = [], [], []

    def create_environment(self):
        return "env-1"

    def create_session(self, agent_id, env, resources, title=None, vault_ids=None):
        if self.fail:
            raise RuntimeError("platform down")
        self.sessions.append({"agent_id": agent_id, "vault_ids": vault_ids})
        return "run-sess-1"

    def send_trigger_and_collect(self, sid, trigger, timeout=None, max_events=None):
        self.triggers.append(trigger)
        return self.events

    def list_pending_approvals(self, sid):
        return self.held

    def approve_action(self, sid, action_id):
        self.approved.append(action_id)

    def reject_action(self, sid, action_id, message=None):
        self.rejected.append(action_id)


def _tool(name):
    return {"type": "agent.mcp_tool_use", "name": name, "text": ""}


class FakeConnect:
    """Stub ConnectClient recording pre-run token refreshes."""
    def __init__(self, boom=False):
        self.refreshed = []
        self.boom = boom

    def refresh_vault(self, vault_ref):
        self.refreshed.append(vault_ref)
        if self.boom:
            raise RuntimeError("pipedream unreachable")
        return {"refreshed": 1, "expires_at": "2026-01-01T00:00:00Z"}


def _agent(owner="usr_1", agent_id="agent-1", staged_apps=None):
    return {"agent_id": agent_id, "owner_user_id": owner, "external_user_id": "x",
            "bundle_id": "wf", "name": "WF", "vault_ref": "vlt-1",
            "trigger": "on a new task", "session_id": "s", "staged_apps": staged_apps or []}


# A staged agent's final message carrying the proposed-actions contract block.
_PROPOSAL_BLOCK = ('```proposed_actions\n'
                   '[{"app": "Gmail", "action": "send email", '
                   '"summary": "Confirm PO-1234 to bob@x.com", '
                   '"details": {"to": "bob@x.com", "subject": "Confirmed", "body": "Hi"}}]\n```')


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

    # --- Spec 8: prompt-level staging -----------------------------------
    def test_staged_agent_proposes_not_executes(self):
        # A staged agent reads freely and PROPOSES its outbound action (no platform
        # hold). run_once parses the proposal into a pending approval, and the staging
        # directive (naming the app) was prepended to the run trigger.
        ma = FakeMA(events=[{"type": "agent.message", "text": _PROPOSAL_BLOCK},
                            {"type": "session.status_idle", "text": ""}])
        rec = runner.run_once(ma, _agent(staged_apps=["gmail"]))
        self.assertEqual(rec["status"], "needs_approval")
        self.assertEqual(len(rec["pending"]), 1)
        p = rec["pending"][0]
        self.assertEqual(p["summary"], "Confirm PO-1234 to bob@x.com")
        self.assertEqual(p["payload"]["to"], "bob@x.com")
        self.assertEqual(p["app"], "Gmail")
        self.assertTrue(p["content_hash"])
        self.assertEqual(ma.approved, [])                      # never executes on the staged run
        self.assertIn("STAGING MODE", ma.triggers[0])          # directive prepended
        self.assertIn("Gmail", ma.triggers[0])

    def test_staged_agent_empty_proposals_completes(self):
        ma = FakeMA(events=[{"type": "agent.message", "text": "```proposed_actions\n[]\n```"},
                            {"type": "session.status_idle", "text": ""}])
        rec = runner.run_once(ma, _agent(staged_apps=["gmail"]))
        self.assertEqual(rec["status"], "completed")
        self.assertFalse(rec.get("pending"))

    def test_staged_agent_malformed_proposal_falls_back_not_dropped(self):
        # If the agent doesn't follow the contract, surface its message for review
        # rather than silently dropping a possible action — and NEVER raise.
        ma = FakeMA(events=[{"type": "agent.message", "text": "I would email Bob about PO-1234."},
                            {"type": "session.status_idle", "text": ""}])
        rec = runner.run_once(ma, _agent(staged_apps=["gmail"]))
        self.assertEqual(rec["status"], "needs_approval")
        self.assertEqual(len(rec["pending"]), 1)

    def test_non_staged_agent_runs_normally(self):
        ma = FakeMA()
        rec = runner.run_once(ma, _agent())                    # no staged_apps
        self.assertEqual(rec["status"], "completed")
        self.assertNotIn("STAGING MODE", ma.triggers[0])       # plain trigger, no directive

    def test_run_once_never_raises_even_if_analysis_breaks(self):
        # Regression for the bogus "Couldn't start the run." — a post-run analysis error
        # must degrade to a recorded failure, not escape run_once.
        orig = runner._build_run_record
        runner._build_run_record = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            rec = runner.run_once(FakeMA(), _agent())
        finally:
            runner._build_run_record = orig
        self.assertEqual(rec["status"], "failed")
        self.assertIn("couldn't read", rec["summary"].lower())

    def test_execute_pending_performs_the_action(self):
        ma = FakeMA(events=[_tool("gmail-send-email"), {"type": "session.status_idle", "text": "Sent."}])
        res = runner.execute_pending(ma, _agent(), {"summary": "Email Bob", "kind": "gmail-send-email",
                                                    "payload": {"to": "bob@x.com"}})
        self.assertEqual(res["status"], "executed")
        self.assertIn("Email Bob", ma.triggers[-1])            # the exact action was instructed
        self.assertIn("perform this ONE action", ma.triggers[-1])

    def test_execute_pending_non_email_write_is_generic(self):
        ma = FakeMA(events=[_tool("google_sheets-add-row"), {"type": "session.status_idle", "text": "Updated."}])
        res = runner.execute_pending(ma, _agent(), {"summary": "Add row to Q3", "kind": "google_sheets-add-row",
                                                    "payload": {"spreadsheet": "Q3", "values": [1, 2, 3]}})
        self.assertEqual(res["status"], "executed")
        self.assertIn("Add row to Q3", ma.triggers[-1])

    def test_execute_pending_failed_on_platform_error(self):
        res = runner.execute_pending(FakeMA(fail=True), _agent(),
                                     {"summary": "x", "kind": "k", "payload": {}})
        self.assertEqual(res["status"], "failed")

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
    @staticmethod
    def _epoch(y, mo, d, hh, mm):
        return datetime.datetime(y, mo, d, hh, mm).timestamp()      # local time, matches schedule_due

    def test_daily(self):
        c = {"kind": "daily", "time": "09:00"}
        before_9 = self._epoch(2026, 6, 24, 8, 0)                   # Wed 08:00
        after_9 = self._epoch(2026, 6, 24, 10, 0)                   # Wed 10:00
        self.assertFalse(runner.schedule_due(c, None, before_9))    # before 9am -> not due
        self.assertTrue(runner.schedule_due(c, None, after_9))      # after 9am, never run -> due
        ran_today = self._epoch(2026, 6, 24, 9, 30)
        self.assertFalse(runner.schedule_due(c, ran_today, after_9))  # already ran today -> not due
        ran_yesterday = self._epoch(2026, 6, 23, 9, 30)
        self.assertTrue(runner.schedule_due(c, ran_yesterday, after_9))  # last ran before today's 9am

    def test_weekly(self):
        c = {"kind": "weekly", "weekday": 0, "time": "09:00"}        # Monday 09:00
        wed = self._epoch(2026, 6, 24, 10, 0)                       # 2026-06-24 is a Wednesday
        self.assertTrue(runner.schedule_due(c, None, wed))          # never run -> due
        ran_this_week = self._epoch(2026, 6, 22, 9, 30)            # Mon of the same week, after anchor
        self.assertFalse(runner.schedule_due(c, ran_this_week, wed))  # already ran this week -> not due
        ran_last_week = self._epoch(2026, 6, 15, 9, 30)           # the previous Monday
        self.assertTrue(runner.schedule_due(c, ran_last_week, wed))   # before this week's anchor -> due
        mon_before_9 = self._epoch(2026, 6, 22, 8, 0)             # Monday but before 09:00
        self.assertFalse(runner.schedule_due(c, ran_last_week, mon_before_9))  # anchor is last Monday; already ran then

    def test_legacy_cadences_still_fire(self):
        # interval/hourly are no longer offered in the UI but remain honored so a
        # pre-existing schedule keeps running.
        self.assertTrue(runner.schedule_due({"kind": "interval", "minutes": 15}, 1000, 1000 + 900))
        self.assertFalse(runner.schedule_due({"kind": "interval", "minutes": 15}, 1000, 1000 + 600))
        self.assertTrue(runner.schedule_due({"kind": "hourly"}, 1000, 1000 + 3600))

    def test_none_never_due(self):
        self.assertFalse(runner.schedule_due(None, None, 1000))


class SchedulerPassTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(self.root, key_store=FakeKeystore())
        self.ma = FakeMA()
        self.state._live_client = lambda: self.ma                     # inject stub client
        self.fc = FakeConnect()
        self.state.connect_fn = lambda: self.fc                       # inject stub connect client

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

    def test_token_refreshed_before_run(self):
        # The scheduled run must refresh the Pipedream token first, so a run long
        # after deploy still authenticates (the bug: it never did).
        registry.agent_upsert(self.root, _agent())          # vault_ref = "vlt-1"
        registry.schedule_set(self.root, "agent-1", "usr_1", {"kind": "hourly"})
        self.state.run_due_schedules(now=time.time())
        self.assertEqual(self.fc.refreshed, ["vlt-1"])       # refreshed before running

    def test_refresh_failure_does_not_block_run(self):
        self.state.connect_fn = lambda: FakeConnect(boom=True)
        rec = self.state.run_agent(_agent())                 # must not raise
        self.assertIn(rec["status"], ("completed", "needs_approval", "failed"))
        self.assertEqual(len(registry.runs_for_agent(self.root, "agent-1")), 1)

    def test_paused_schedule_does_not_run(self):
        registry.agent_upsert(self.root, _agent())
        registry.schedule_set(self.root, "agent-1", "usr_1", {"kind": "hourly"}, paused=True)
        self.state.run_due_schedules(now=time.time())
        self.assertEqual(registry.runs_for_agent(self.root, "agent-1"), [])

    # --- Spec 8: staged actions persist, dedupe, and execute on approval ---
    def _staging_ma(self):
        # a staged agent that proposes one Gmail action (prompt-level staging)
        return FakeMA(events=[{"type": "agent.message", "text": _PROPOSAL_BLOCK},
                              {"type": "session.status_idle", "text": ""}])

    def _staged_agent(self):
        registry.agent_upsert(self.root, _agent(staged_apps=["gmail"]))
        return _agent(staged_apps=["gmail"])

    def test_staged_run_persists_pending_and_dedupes(self):
        agent = self._staged_agent()
        self.state._live_client = lambda: self._staging_ma()
        self.state.run_agent(agent)
        self.state.run_agent(agent)                           # identical proposal again
        pend = registry.pending_for_user(self.root, "usr_1")
        self.assertEqual(len(pend), 1)                        # deduped, not re-staged
        self.assertEqual(pend[0]["payload"]["to"], "bob@x.com")
        self.assertEqual(registry.pending_count_for_user(self.root, "usr_1"), 1)

    def test_approve_pending_executes_and_marks_processed(self):
        agent = self._staged_agent()
        ma = self._staging_ma()
        self.state._live_client = lambda: ma
        self.state.run_agent(agent)
        item = registry.pending_for_user(self.root, "usr_1")[0]
        res = self.state.approve_pending(item)
        self.assertEqual(res["status"], "executed")           # Trace performed it (fresh task)
        self.assertEqual(registry.pending_count_for_user(self.root, "usr_1"), 0)
        self.assertEqual(registry.pending_get(self.root, item["id"])["status"], "executed")

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
