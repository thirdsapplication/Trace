"""Spec 4 — Trace-side scheduler: the headless run path (runner), cadence
due-logic, a scheduler pass that runs a due agent and records it, schedule
durability across a restart, and the desktop-node guard. Offline against a stub
MA client + file-mode registry (no DATABASE_URL).
"""

import datetime
import json
import os
import tempfile
import time
import unittest

from trace.ui import app, registry
from trace.agent import runner
from trace.tests.test_ui import FakeKeystore


class FakeMA:
    """Minimal Managed Agents client for the run path. Sessions get DISTINCT ids
    (run-sess-1, run-sess-2, …) and events can differ per session via session_events —
    the approval-resurrection regression only reproduces when the execution session is
    distinguishable from the session that proposed."""
    def __init__(self, events=None, held=None, fail=False):
        self.events = events if events is not None else [{"type": "session.status_idle", "text": "Handled 2 emails"}]
        self.held = held or []
        self.fail = fail
        self.sessions = []
        self.session_events = {}
        self.approved, self.rejected, self.triggers = [], [], []

    def create_environment(self, defaults=None):
        self.environments = getattr(self, "environments", [])
        self.environments.append((defaults or {}).get("name"))
        return "env-1"

    def create_session(self, agent_id, env, resources, title=None, vault_ids=None):
        if self.fail:
            raise RuntimeError("platform down")
        self.sessions.append({"agent_id": agent_id, "vault_ids": vault_ids,
                              "resources": resources, "title": title})
        return f"run-sess-{len(self.sessions)}"

    def send_trigger_and_collect(self, sid, trigger, timeout=None, max_events=None):
        self.triggers.append(trigger)
        return self.session_events.get(sid, self.events)

    def list_session_events(self, session_id):
        self.listed = getattr(self, "listed", 0) + 1
        return self.session_events.get(session_id, self.events)

    def list_pending_approvals(self, sid):
        return self.held

    def approve_action(self, sid, action_id):
        self.approved.append(action_id)

    def reject_action(self, sid, action_id, message=None):
        self.rejected.append(action_id)

    # --- platform deployments (additive; used by the sync/run-now tests) ---
    def __init_deployments(self):
        if not hasattr(self, "druns"):
            self.druns, self.manual_runs = [], []
            self.archived, self.paused_deps, self.unpaused_deps = [], [], []
            self.created_deployments = []

    def run_deployment(self, deployment_id):
        self.__init_deployments()
        self.manual_runs.append(deployment_id)
        return {"id": f"drun_m{len(self.manual_runs)}",
                "session_id": f"dep_sess_m{len(self.manual_runs)}",
                "error_type": None, "error_message": None, "created_at": "now"}

    def list_deployment_runs(self, deployment_id):
        self.__init_deployments()
        return list(self.druns)

    def archive_deployment(self, deployment_id):
        self.__init_deployments()
        self.archived.append(deployment_id)
        return True

    def pause_deployment(self, deployment_id):
        self.__init_deployments()
        self.paused_deps.append(deployment_id)
        return True

    def unpause_deployment(self, deployment_id):
        self.__init_deployments()
        self.unpaused_deps.append(deployment_id)
        return True

    def create_deployment(self, name, agent_id, environment_id, trigger_text,
                          schedule=None, resources=None, vault_ids=None, metadata=None):
        self.__init_deployments()
        dep = {"id": f"dep_{len(self.created_deployments)}", "name": name,
               "agent_id": agent_id, "trigger_text": trigger_text, "schedule": schedule,
               "resources": resources, "vault_ids": vault_ids, "metadata": metadata}
        self.created_deployments.append(dep)
        return dep["id"]


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

# Gap #3: a final message where the agent paused on a blocking edge case.
_NEEDS_INPUT_BLOCK = ('```needs_input\n'
                      '[{"question": "PO-1234 has no ship date — assume 5-day or ask buyer?", '
                      '"context": "Inbound email omits a ship date; item is in stock.", '
                      '"options": ["Assume 5-day", "Ask the buyer"]}]\n```')


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

    # --- prompt-level staging: run_once triggers; proposals come from activity ---
    def test_staged_agent_gets_staging_directive(self):
        # A staged agent's run prepends the staging directive (naming the app) so it
        # proposes instead of executing; run_once itself just triggers + records.
        ma = FakeMA(events=[{"type": "agent.message", "text": _PROPOSAL_BLOCK},
                            {"type": "session.thread_status_idle", "stop_reason": {"type": "end_turn"}}])
        rec = runner.run_once(ma, _agent(staged_apps=["gmail"]))
        self.assertIn("STAGING MODE", ma.triggers[0])
        self.assertIn("Gmail", ma.triggers[0])
        self.assertEqual(rec["run_session_id"], "run-sess-1")
        self.assertEqual(ma.approved, [])                      # never executes on the staged run

    def test_non_staged_agent_runs_normally(self):
        ma = FakeMA()
        rec = runner.run_once(ma, _agent())                    # no staged_apps
        self.assertEqual(rec["status"], "completed")
        self.assertNotIn("STAGING MODE", ma.triggers[0])       # plain trigger, no directive

    def test_run_once_preserves_session_id_when_collect_fails(self):
        # Regression: the bogus "Couldn't run" that DISCARDED the real session id — the
        # session is created, so its id must survive for the activity view to read it.
        class Boom(FakeMA):
            def send_trigger_and_collect(self, sid, trigger, timeout=None, max_events=None):
                raise RuntimeError("400 stream error")
        rec = runner.run_once(Boom(), _agent())
        self.assertEqual(rec["run_session_id"], "run-sess-1")  # preserved, not None
        self.assertNotEqual(rec["status"], "failed")           # triggered; just couldn't collect

    def test_run_once_never_raises_even_if_analysis_breaks(self):
        orig = runner._build_run_record
        runner._build_run_record = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            rec = runner.run_once(FakeMA(), _agent())
        finally:
            runner._build_run_record = orig
        self.assertEqual(rec["run_session_id"], "run-sess-1")  # id preserved on degrade

    def test_execute_actions_batches_selected(self):
        ma = FakeMA(events=[_tool("gmail-send-email"), {"type": "session.status_idle", "text": "Done."}])
        res = runner.execute_actions(ma, _agent(), [
            {"summary": "Email Bob", "kind": "gmail-send-email", "payload": {"to": "bob@x.com"}},
            {"summary": "Add ERP row", "kind": "google_sheets-add-row", "payload": {"row": 1}}])
        self.assertEqual(res["status"], "executed")
        self.assertIn("Email Bob", ma.triggers[-1])
        self.assertIn("Add ERP row", ma.triggers[-1])

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

    # --- Gap #3: uncertainty handling (agent-initiated pause) ---------------
    def test_uncertainty_directive_on_every_run(self):
        # Even a plain, non-staged run carries the uncertainty directive so the agent
        # can pause when genuinely stuck.
        ma = FakeMA()
        runner.run_once(ma, _agent())
        self.assertIn("needs_input", ma.triggers[0])
        self.assertIn(str(runner._uncertainty_max_questions()), ma.triggers[0])

    def test_needs_input_run_gets_needs_input_status(self):
        ma = FakeMA(events=[{"type": "agent.message", "text": _NEEDS_INPUT_BLOCK},
                            {"type": "session.thread_status_idle",
                             "stop_reason": {"type": "end_turn"}}])
        rec = runner.run_once(ma, _agent())
        self.assertEqual(rec["status"], "needs_input")
        self.assertEqual(len(rec["questions"]), 1)
        self.assertIn("ship date", rec["details"][0])
        self.assertEqual(rec["questions"][0]["options"], ["Assume 5-day", "Ask the buyer"])

    def test_parse_needs_input_requires_fence_and_caps(self):
        # a bare array (no needs_input fence) is NOT read as questions
        self.assertEqual(runner._parse_needs_input('[{"question": "x"}]'), [])
        # a proposals block is not misread as questions
        self.assertEqual(runner._parse_needs_input(_PROPOSAL_BLOCK), [])
        # capped at the interview limit
        cap = runner._uncertainty_max_questions()
        many = ", ".join('{"question": "q%d"}' % i for i in range(cap + 5))
        parsed = runner._parse_needs_input("```needs_input\n[" + many + "]\n```")
        self.assertEqual(len(parsed), cap)

    def test_needs_input_status_ranks_above_staged(self):
        # a message with BOTH a question and a proposal reads as needs_input (answer first)
        ma = FakeMA(events=[{"type": "agent.message",
                             "text": _NEEDS_INPUT_BLOCK + "\n" + _PROPOSAL_BLOCK},
                            {"type": "session.thread_status_idle",
                             "stop_reason": {"type": "end_turn"}}])
        rec = runner.run_once(ma, _agent())
        self.assertEqual(rec["status"], "needs_input")

    # --- Gap #3 B4: retrieved lessons injected into the run trigger ----------
    def test_guidance_injected_into_trigger(self):
        ma = FakeMA()
        runner.run_once(ma, _agent(),
                        guidance=["When an order lacks a ship date, ask the customer."])
        self.assertIn("LESSONS FROM PAST HUMAN DECISIONS", ma.triggers[0])
        self.assertIn("ask the customer", ma.triggers[0])

    def test_no_guidance_means_no_lessons_block(self):
        ma = FakeMA()
        runner.run_once(ma, _agent())
        self.assertNotIn("LESSONS FROM PAST HUMAN DECISIONS", ma.triggers[0])


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

    def test_activity_summarizes_surfaces_proposals_and_dedupes(self):
        agent = self._staged_agent()
        self.state._live_client = lambda: self._staging_ma()
        self.state.summarize_fn = lambda system, user: "Checked the inbox; proposed actions."
        self.state.run_agent(agent)                           # records a run with a session id
        # Reading the session's events and summarizing them is backgrounded now (it is
        # what made this page slow); the view itself renders what that refresh wrote.
        self.state.refresh_activity(agent)
        act = self.state.agent_activity(agent)
        self.assertTrue(act)
        self.assertEqual(act[0]["status"], "staged")          # status read from real events
        self.assertEqual(act[0]["summary"], "Checked the inbox; proposed actions.")
        self.assertEqual(len(act[0]["proposals"]), 1)         # proposal surfaced for approval
        self.assertEqual(act[0]["proposals"][0]["payload"]["to"], "bob@x.com")
        self.state.agent_activity(agent)                      # re-open
        self.assertEqual(registry.pending_count_for_user(self.root, "usr_1"), 1)  # deduped

    def test_activity_summary_is_cached(self):
        agent = self._staged_agent()
        self.state._live_client = lambda: self._staging_ma()
        calls = {"n": 0}
        def summ(system, user):
            calls["n"] += 1
            return "A summary."
        self.state.summarize_fn = summ
        self.state.run_agent(agent)
        self.state.refresh_activity(agent)
        self.state.refresh_activity(agent)
        self.assertEqual(calls["n"], 1)                       # summarized once, then cached
        self.assertEqual(self.state.agent_activity(agent)[0]["summary"], "A summary.")

    def test_approve_actions_executes_selected_and_marks_processed(self):
        agent = self._staged_agent()
        ma = self._staging_ma()
        self.state._live_client = lambda: ma                  # ONE client: session ids stay distinct
        self.state.summarize_fn = lambda s, u: "x"
        self.state.run_agent(agent)                           # run-sess-1 proposes
        self.state.agent_activity(agent)                      # creates the pending item
        pend = registry.pending_for_user(self.root, "usr_1")
        self.assertEqual(len(pend), 1)
        res = self.state.approve_actions(agent, [pend[0]["id"]])
        self.assertEqual(res["status"], "executed")
        self.assertEqual(registry.pending_count_for_user(self.root, "usr_1"), 0)
        self.assertEqual(registry.pending_get(self.root, pend[0]["id"])["status"], "executed")
        # The reload. The execution session's transcript is a prose completion report —
        # exactly what used to be manufactured back into a fresh "pending" proposal
        # with live Approve buttons.
        ma.session_events["run-sess-2"] = [
            {"type": "agent.message", "text": "Both actions are complete: sent the email."},
            {"type": "session.status_idle", "text": ""}]
        act = self.state.agent_activity(agent)
        self.assertEqual(registry.pending_count_for_user(self.root, "usr_1"), 0)  # no phantom
        by_sid = {e["run_session_id"]: e for e in act}
        self.assertEqual(by_sid["run-sess-2"]["proposals"], [])   # approval run: never mined
        self.assertEqual(registry.runs_for_agent(self.root, "agent-1")[0]["kind"], "approval")
        origin = by_sid["run-sess-1"]["proposals"]                # decided card still renders…
        self.assertEqual([p["status"] for p in origin], ["executed"])  # …as its outcome
        # And the id can't be approved twice.
        res2 = self.state.approve_actions(agent, [pend[0]["id"]])
        self.assertEqual(res2["summary"], "Nothing to approve.")

    def test_rejected_and_failed_proposals_do_not_resurrect_on_reload(self):
        # The source session's transcript keeps its proposals fence forever and every
        # activity view re-reads it — a decided proposal must stay decided.
        ma = self._staging_ma()                               # shared: session ids stay unique
        self.state._live_client = lambda: ma
        self.state.summarize_fn = lambda s, u: "x"
        for i, outcome in enumerate(("rejected", "failed"), start=1):
            with self.subTest(outcome=outcome):
                # A distinct agent per outcome: the content hash covers agent_id, and
                # the widened dedupe would (correctly) swallow a same-agent restage.
                agent = _agent(agent_id=f"agent-{outcome}", staged_apps=["gmail"])
                registry.agent_upsert(self.root, agent)
                self.state.run_agent(agent)                   # -> run-sess-<i>
                self.state.agent_activity(agent)
                pend = [p for p in registry.pending_for_user(self.root, "usr_1")
                        if p["agent_id"] == agent["agent_id"]]
                registry.pending_set_status(self.root, pend[0]["id"], outcome)
                for _ in range(2):                            # two reloads, no resurrection
                    act = self.state.agent_activity(agent)
                self.assertEqual(registry.pending_count_for_user(self.root, "usr_1"), 0)
                props = {e["run_session_id"]: e for e in act}[f"run-sess-{i}"]["proposals"]
                self.assertEqual([p["status"] for p in props], [outcome])

    def test_pending_claim_is_exclusive(self):
        agent = self._staged_agent()
        ma = self._staging_ma()
        self.state._live_client = lambda: ma
        self.state.summarize_fn = lambda s, u: "x"
        self.state.run_agent(agent)
        self.state.agent_activity(agent)
        pid = registry.pending_for_user(self.root, "usr_1")[0]["id"]
        self.assertIsNone(registry.pending_claim(self.root, pid, "someone_else"))
        first = registry.pending_claim(self.root, pid, "usr_1")
        self.assertEqual(first["status"], "executing")
        self.assertIsNone(registry.pending_claim(self.root, pid, "usr_1"))  # double click

    def test_pending_claim_to_status_queued(self):
        # The approval queue claims into "queued" (send happens later, on the
        # agent's worker) — same atomic pending-only predicate, same exclusivity.
        registry.pending_add(self.root, {
            "agent_id": "agent-1", "owner_user_id": "usr_1", "run_session_id": "rs",
            "app": "gmail", "kind": "gmail-send-email", "summary": "Email Bob",
            "payload": {}, "content_hash": "hq1"})
        pid = registry.pending_for_user(self.root, "usr_1")[0]["id"]
        it = registry.pending_claim(self.root, pid, "usr_1", to_status="queued")
        self.assertEqual(it["status"], "queued")
        self.assertIsNone(registry.pending_claim(self.root, pid, "usr_1"))  # double click

    def test_release_queued_on_boot(self):
        # Boot recovery: a "queued" row's in-memory queue died with the previous
        # process and its action was never sent — release it back to approvable.
        # An "executing" row is left for the stale reaper (outcome unknown).
        for i, h in enumerate(("hr1", "hr2"), 1):
            registry.pending_add(self.root, {
                "agent_id": "agent-1", "owner_user_id": "usr_1", "run_session_id": "rs",
                "app": "gmail", "kind": "gmail-send-email", "summary": f"Email {i}",
                "payload": {}, "content_hash": h})
        p1, p2 = sorted(registry.pending_for_user(self.root, "usr_1"),
                        key=lambda p: int(p["id"]))
        registry.pending_claim(self.root, p1["id"], "usr_1", to_status="queued")
        registry.pending_claim(self.root, p2["id"], "usr_1")            # executing
        registry.pending_release_queued(self.root)
        r1 = registry.pending_get(self.root, p1["id"])
        self.assertEqual(r1["status"], "pending")                       # approvable again
        self.assertIsNone(r1["processed_at"])
        self.assertEqual(registry.pending_get(self.root, p2["id"])["status"], "executing")

    def test_stale_executing_claim_settles_failed_on_view(self):
        # A claim orphaned by a crash mid-execution must not say "Sending…" forever:
        # the activity view settles anything executing for >15 minutes to failed.
        agent = self._staged_agent()
        ma = self._staging_ma()
        self.state._live_client = lambda: ma
        self.state.summarize_fn = lambda s, u: "x"
        self.state.run_agent(agent)
        self.state.agent_activity(agent)
        pid = registry.pending_for_user(self.root, "usr_1")[0]["id"]
        registry.pending_claim(self.root, pid, "usr_1")
        path = os.path.join(self.root, "_registry", "pending.json")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        d[str(pid)]["processed_at"] = (datetime.datetime.now().astimezone()
                                       - datetime.timedelta(minutes=16)).isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        act = self.state.agent_activity(agent)
        self.assertEqual(registry.pending_get(self.root, pid)["status"], "failed")
        props = {e["run_session_id"]: e for e in act}["run-sess-1"]["proposals"]
        self.assertEqual([p["status"] for p in props], ["failed"])

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


# --- MA deployments: cron mapping, sync loop, run-now, memory attachment ----
from trace.agent import config as agent_config  # noqa: E402


def _dep_agent(agent_id="agent-d1", deployment_id="dep_live", store="mem_live",
               owner="usr_1", staged_apps=None):
    return {**_agent(owner=owner, agent_id=agent_id, staged_apps=staged_apps),
            "deployment_id": deployment_id, "memory_store_id": store}


class CadenceToCronTest(unittest.TestCase):
    def test_daily_and_weekly_honor_stored_time(self):
        self.assertEqual(agent_config.cadence_to_cron({"kind": "daily", "time": "09:00"}),
                         {"expression": "0 9 * * *",
                          "timezone": agent_config.TRACE_SCHEDULE_TZ})
        # Trace weekday 6 (Sunday) -> cron 0 (Sunday); stored time honored
        self.assertEqual(
            agent_config.cadence_to_cron({"kind": "weekly", "weekday": 6, "time": "07:30"}),
            {"expression": "30 7 * * 0", "timezone": agent_config.TRACE_SCHEDULE_TZ})
        self.assertEqual(
            agent_config.cadence_to_cron({"kind": "weekly", "weekday": 0, "time": "09:00"})
            ["expression"], "0 9 * * 1")                     # Monday -> cron 1

    def test_legacy_and_unmappable(self):
        self.assertEqual(agent_config.cadence_to_cron({"kind": "hourly"})["expression"],
                         "0 * * * *")
        self.assertIsNone(agent_config.cadence_to_cron({"kind": "interval", "minutes": 15}))
        self.assertIsNone(agent_config.cadence_to_cron(None))

    def test_tolerant_time_parsing(self):
        self.assertEqual(agent_config.cadence_to_cron({"kind": "daily", "time": "junk"})
                         ["expression"], "0 9 * * *")        # falls back to 09:00


class DeploymentSyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(self.root, key_store=FakeKeystore())
        self.ma = FakeMA()
        self.state._live_client = lambda: self.ma
        self.fc = FakeConnect()
        self.state.connect_fn = lambda: self.fc

    def tearDown(self):
        self.state.scheduler_stop.set()
        self._tmp.cleanup()

    def test_run_due_skips_deployment_agents(self):
        # the platform crons deployment agents; the legacy pass must not double-run them
        registry.agent_upsert(self.root, _dep_agent())
        registry.schedule_set(self.root, "agent-d1", "usr_1", {"kind": "hourly"})
        self.state.run_due_schedules(now=time.time())
        self.assertEqual(self.ma.sessions, [])               # no legacy run composed
        self.assertEqual(self.ma.triggers, [])

    def test_sync_records_unseen_druns_and_dedupes(self):
        registry.agent_upsert(self.root, _dep_agent())
        self.ma.list_deployment_runs("x")                    # init the fake's drun store
        self.ma.druns = [
            {"id": "drun_1", "session_id": "sess_a", "error_type": None,
             "error_message": None, "created_at": "t1"},
            {"id": "drun_2", "session_id": None, "error_type": "vault_not_found",
             "error_message": "vault vlt-1 was deleted", "created_at": "t2"},
        ]
        self.state.sync_deployment_runs(now=time.time())
        runs = registry.runs_for_agent(self.root, "agent-d1")
        self.assertEqual(len(runs), 2)
        by_drun = {r["drun_id"]: r for r in runs}
        self.assertEqual(by_drun["drun_1"]["status"], "ran")
        self.assertEqual(by_drun["drun_1"]["run_session_id"], "sess_a")
        self.assertEqual(by_drun["drun_2"]["status"], "failed")
        self.assertIn("vault", by_drun["drun_2"]["summary"])
        # a second pass sees both drun ids as recorded -> no duplicates (failed too)
        self.state.sync_deployment_runs(now=time.time())
        self.assertEqual(len(registry.runs_for_agent(self.root, "agent-d1")), 2)

    def test_sync_vault_refresh_throttled(self):
        registry.agent_upsert(self.root, _dep_agent())
        t0 = time.time()
        self.state.sync_deployment_runs(now=t0)
        self.state.sync_deployment_runs(now=t0 + 60)          # 1 min later: throttled
        self.assertEqual(self.fc.refreshed, ["vlt-1"])
        self.state.sync_deployment_runs(now=t0 + self.state.VAULT_REFRESH_SECONDS + 1)
        self.assertEqual(self.fc.refreshed, ["vlt-1", "vlt-1"])  # window elapsed -> again

    def test_sync_survives_one_bad_agent(self):
        registry.agent_upsert(self.root, _dep_agent(agent_id="agent-bad", deployment_id="dep_bad"))
        registry.agent_upsert(self.root, _dep_agent(agent_id="agent-ok", deployment_id="dep_ok"))
        real = self.ma.list_deployment_runs
        self.ma.druns = [{"id": "drun_9", "session_id": "s9", "error_type": None,
                          "error_message": None, "created_at": "t"}]
        def maybe_boom(dep_id):
            if dep_id == "dep_bad":
                raise RuntimeError("platform hiccup")
            return real(dep_id)
        self.ma.list_deployment_runs = maybe_boom
        self.state.sync_deployment_runs(now=time.time())      # must not raise
        self.assertEqual(len(registry.runs_for_agent(self.root, "agent-ok")), 1)

    def test_run_now_uses_deployment_run(self):
        registry.agent_upsert(self.root, _dep_agent())
        rec = self.state.run_deployment_now(_dep_agent())
        self.assertEqual(rec["status"], "ran")
        self.assertEqual(self.ma.manual_runs, ["dep_live"])
        runs = registry.runs_for_agent(self.root, "agent-d1")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["run_session_id"].startswith("dep_sess_"))
        self.assertEqual(self.fc.refreshed, ["vlt-1"])        # vault refreshed first
        self.assertEqual(self.ma.sessions, [])                # no legacy session composed

    def test_run_now_failure_recorded_never_raises(self):
        self.ma.run_deployment = lambda dep_id: (_ for _ in ()).throw(RuntimeError("boom"))
        rec = self.state.run_deployment_now(_dep_agent())
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(registry.runs_for_agent(self.root, "agent-d1")[0]["status"], "failed")

    def test_apply_platform_schedule_archives_and_recreates(self):
        registry.agent_upsert(self.root, _dep_agent())
        new_id = self.state.apply_platform_schedule(
            _dep_agent(), {"kind": "daily", "time": "09:00"})
        self.assertEqual(self.ma.archived, ["dep_live"])      # old one archived
        dep = self.ma.created_deployments[0]
        self.assertEqual(dep["schedule"]["expression"], "0 9 * * *")
        self.assertEqual(dep["vault_ids"], ["vlt-1"])
        self.assertEqual(dep["resources"][0]["memory_store_id"], "mem_live")
        self.assertIn(agent_config.MEMORY_UPDATE_DIRECTIVE, dep["trigger_text"])
        # the registry row now points at the NEW deployment
        row = next(a for a in registry.agents_all(self.root) if a["agent_id"] == "agent-d1")
        self.assertEqual(row["deployment_id"], new_id)

    def test_apply_platform_schedule_none_creates_manual(self):
        registry.agent_upsert(self.root, _dep_agent())
        self.state.apply_platform_schedule(_dep_agent(), None)
        self.assertIsNone(self.ma.created_deployments[0]["schedule"])  # manual: run-now works

    def test_apply_platform_schedule_creates_before_archiving(self):
        # create must succeed and be registered BEFORE the old one is archived, so a
        # create failure can never strand the agent on an archived deployment.
        registry.agent_upsert(self.root, _dep_agent())
        order = []
        real_archive, real_create = self.ma.archive_deployment, self.ma.create_deployment
        self.ma.archive_deployment = lambda d: (order.append(("archive", d)), real_archive(d))[1]
        self.ma.create_deployment = lambda **kw: (order.append(("create", None)), real_create(**kw))[1]
        self.state.apply_platform_schedule(_dep_agent(), {"kind": "daily", "time": "09:00"})
        self.assertEqual([o[0] for o in order], ["create", "archive"])   # create first

    def test_failed_schedule_change_leaves_old_deployment_registered(self):
        registry.agent_upsert(self.root, _dep_agent())          # deployment_id = dep_live
        self.ma.list_deployment_runs("boot")                    # init the fake's stores
        self.ma.create_deployment = lambda **kw: (_ for _ in ()).throw(RuntimeError("cron bad"))
        with self.assertRaises(RuntimeError):
            self.state.apply_platform_schedule(_dep_agent(), {"kind": "daily", "time": "09:00"})
        # old deployment NOT archived and still registered -> the working schedule survives
        self.assertEqual(self.ma.archived, [])
        row = next(a for a in registry.agents_all(self.root) if a["agent_id"] == "agent-d1")
        self.assertEqual(row["deployment_id"], "dep_live")

    def test_run_now_and_sync_record_same_drun_once(self):
        # the run-now drun is also visible to the sync pass; it must be recorded exactly once
        registry.agent_upsert(self.root, _dep_agent())
        self.state.run_deployment_now(_dep_agent())             # records drun_m1 for dep_sess_m1
        # sync now lists that SAME drun; it must not double-record
        self.ma.druns = [{"id": "drun_m1", "session_id": "dep_sess_m1", "error_type": None,
                          "error_message": None, "created_at": "t"}]
        self.state.sync_deployment_runs(now=time.time())
        runs = registry.runs_for_agent(self.root, "agent-d1")
        self.assertEqual([r["drun_id"] for r in runs], ["drun_m1"])   # exactly one

    def test_interval_deployment_agent_falls_through_to_trace_scheduler(self):
        # an interval cadence has no platform cron mapping; a deployment agent with it
        # must still run (via the Trace scheduler), not silently never fire.
        registry.agent_upsert(self.root, _dep_agent())
        registry.schedule_set(self.root, "agent-d1", "usr_1",
                              {"kind": "interval", "minutes": 15})
        self.state.run_due_schedules(now=time.time())
        # ran via the legacy path (a session was composed), not skipped
        self.assertTrue(self.ma.sessions)


class RunnerMemoryTest(unittest.TestCase):
    def test_run_once_attaches_memory_and_directive(self):
        ma = FakeMA()
        runner.run_once(ma, _dep_agent())
        self.assertEqual(ma.sessions[0]["resources"][0]["memory_store_id"], "mem_live")
        self.assertIn(agent_config.MEMORY_UPDATE_DIRECTIVE, ma.triggers[0])

    def test_run_once_without_store_unchanged(self):
        ma = FakeMA()
        runner.run_once(ma, _agent())
        self.assertEqual(ma.sessions[0]["resources"], [])
        self.assertNotIn(agent_config.MEMORY_UPDATE_DIRECTIVE, ma.triggers[0])

    def test_execute_pending_attaches_memory_and_directive(self):
        ma = FakeMA()
        pending = {"summary": "send the reply", "payload": {"to": "a@b.com"}}
        rec = runner.execute_pending(ma, _dep_agent(), pending)
        self.assertEqual(rec["status"], "executed")
        self.assertEqual(ma.sessions[0]["resources"][0]["type"], "memory_store")
        self.assertIn(agent_config.MEMORY_UPDATE_DIRECTIVE, ma.triggers[0])

    def test_run_names_identify_the_user(self):
        # session titles + environment names carry owner identity + agent + purpose,
        # so an administrator can tell whose run this is in the Console backend
        ma = FakeMA()
        agent = {**_agent(), "owner_email": "jane@acme.com", "name": "Order intake"}
        runner.run_once(ma, agent)
        self.assertEqual(ma.sessions[0]["title"],
                         "Trace · jane@acme.com · Order intake · scheduled run")
        self.assertEqual(ma.environments[-1], "trace-jane-acme-com-order-intake-env")
        # rows deployed before owner_email existed fall back to the external id
        ma2 = FakeMA()
        runner.run_once(ma2, _agent())
        self.assertEqual(ma2.sessions[0]["title"], "Trace · x · WF · scheduled run")


class RegistryDeploymentFileModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_agents_all_and_with_deployments_and_set_deployment(self):
        registry.agent_upsert(self.root, _agent(agent_id="legacy-1"))
        registry.agent_upsert(self.root, _dep_agent(agent_id="dep-1"))
        self.assertEqual({a["agent_id"] for a in registry.agents_all(self.root)},
                         {"legacy-1", "dep-1"})
        self.assertEqual([a["agent_id"] for a in registry.agents_with_deployments(self.root)],
                         ["dep-1"])
        registry.agent_set_deployment(self.root, "dep-1", "dep_new")
        row = next(a for a in registry.agents_all(self.root) if a["agent_id"] == "dep-1")
        self.assertEqual(row["deployment_id"], "dep_new")

    def test_run_seen_drun_ids(self):
        registry.run_add(self.root, {"agent_id": "a1", "owner_user_id": "u",
                                     "status": "ran", "summary": "s", "details": [],
                                     "run_session_id": "sess", "drun_id": "drun_1"})
        registry.run_add(self.root, {"agent_id": "a1", "owner_user_id": "u",
                                     "status": "completed", "summary": "s", "details": [],
                                     "run_session_id": "sess2"})     # legacy run, no drun
        self.assertEqual(registry.run_seen_drun_ids(self.root, "a1"), {"drun_1"})
        self.assertEqual(registry.run_seen_drun_ids(self.root, "missing"), set())


if __name__ == "__main__":
    unittest.main()
