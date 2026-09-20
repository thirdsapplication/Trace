"""The activity engine: summarize real session logs (injected model_fn) + extract
staged proposed actions + classify the session outcome. Offline, no platform."""

import json
import unittest

from trace.agent import activity

# Event shapes mirror real MA session logs (agent.message text, mcp_tool_use, results,
# a terminal thread_status_idle with a nested stop_reason).
_PROPOSAL = ('```proposed_actions\n'
             '[{"app": "Google Sheets", "action": "add row", '
             '"summary": "Add ERP row for ORD-029", "details": {"sheet": "Open_Orders", "order": "ORD-029"}},'
             ' {"app": "Gmail", "action": "send reply", '
             '"summary": "Confirm order to Field Oak Bistro", "details": {"to": "bistro@x.com"}}]\n```')


def _staged_session():
    return [
        {"type": "user.message", "text": "STAGING MODE — read freely ... Task: on a new matching task"},
        {"type": "agent.message", "text": "I'll check the inbox for new orders."},
        {"type": "agent.mcp_tool_use", "name": "gmail-find-email", "input": {"q": "subject:order"}},
        {"type": "agent.mcp_tool_result", "text": '{"ret":[{"id":"19ef"}]}', "is_error": False},
        {"type": "agent.message", "text": _PROPOSAL},
        {"type": "session.thread_status_idle", "stop_reason": {"type": "end_turn"}},
    ]


_NEEDS_INPUT = ('```needs_input\n'
                '[{"question": "Which sheet gets ORD-029 — Open or Archive?", '
                '"context": "Two matching sheets exist; the order maps to neither cleanly."}]\n```')


def _needs_input_session():
    return [
        {"type": "user.message", "text": "on a new matching task"},
        {"type": "agent.message", "text": "I found an order but can't place it."},
        {"type": "agent.message", "text": _NEEDS_INPUT},
        {"type": "session.thread_status_idle", "stop_reason": {"type": "end_turn"}},
    ]


def _executed_session():
    return [
        {"type": "user.message", "text": "on a new matching task"},
        {"type": "agent.message", "text": "No new orders found in the inbox."},
        {"type": "agent.mcp_tool_use", "name": "gmail-find-email", "input": {"q": "subject:order"}},
        {"type": "agent.mcp_tool_result", "text": "{\"ret\":[]}", "is_error": False},
        {"type": "session.thread_status_idle", "stop_reason": {"type": "end_turn"}},
    ]


class ActivityEngineTest(unittest.TestCase):
    def test_extract_proposals_from_final_block(self):
        props = activity.extract_proposals(_staged_session())
        self.assertEqual(len(props), 2)
        self.assertEqual(props[0]["app"], "Google Sheets")
        self.assertEqual(props[1]["details"]["to"], "bistro@x.com")

    def test_extract_proposals_none_when_absent(self):
        self.assertEqual(activity.extract_proposals(_executed_session()), [])

    def test_session_status(self):
        self.assertEqual(activity.session_status(_staged_session()), "staged")
        self.assertEqual(activity.session_status(_executed_session()), "completed")
        self.assertEqual(activity.session_status(
            [{"type": "agent.message", "text": "working"}]), "running")  # no terminal yet
        self.assertEqual(activity.session_status(
            [{"type": "session.status_failed"}]), "failed")

    def test_extract_questions_and_needs_input_status(self):
        qs = activity.extract_questions(_needs_input_session())
        self.assertEqual(len(qs), 1)
        self.assertIn("Open or Archive", qs[0]["question"])
        self.assertEqual(activity.session_status(_needs_input_session()), "needs_input")
        self.assertEqual(activity.extract_questions(_executed_session()), [])

    def test_summarize_builds_compact_prompt_and_returns_text(self):
        seen = {}
        def model_fn(system, user):
            seen["system"], seen["user"] = system, user
            return "  Checked Gmail, found 1 order, proposed 2 actions.  "
        out = activity.summarize_session(_staged_session(), "Order agent", model_fn)
        self.assertEqual(out, "Checked Gmail, found 1 order, proposed 2 actions.")
        # the compacted transcript carries the agent text + tool calls, drops nothing vital
        self.assertIn("gmail-find-email", seen["user"])
        self.assertIn("[agent]", seen["user"])
        self.assertIn("Order agent", seen["user"])

    def test_summarize_best_effort_on_model_error(self):
        def boom(system, user):
            raise RuntimeError("model down")
        self.assertEqual(activity.summarize_session(_staged_session(), "a", boom), "")

    def test_structured_account_is_rendered_to_the_one_paragraph_the_view_shows(self):
        """v2: the model returns {headline, actions, needs_you, problems, status}; the
        view and the summary store still hold one plain paragraph, so the account is
        rendered into that — empty sections omitted — and the run mode reaches the
        prompt so a staged run's proposals read as waiting on the worker."""
        seen = {}

        def model_fn(system, user):
            seen["system"], seen["user"] = system, user
            return json.dumps({
                "headline": "Entered 2 orders into the sheet",
                "actions": [{"text": "Added order 12 as one row", "ref": None}],
                "needs_you": [{"type": "proposal", "text": "Reply confirming order 12 — awaiting approval"}],
                "problems": [],
                "status": "waiting"})
        out = activity.summarize_session(_staged_session(), "Order agent", model_fn, mode="staged")
        self.assertEqual(out, "Entered 2 orders into the sheet. Done: Added order 12 as one row. "
                              "Needs you: Reply confirming order 12 — awaiting approval.")
        self.assertIn("Mode: staged", seen["user"])
        self.assertTrue(seen["system"].startswith("## Trace"))
        self.assertNotIn("## How capture works", seen["system"])    # lite foundation only
        self.assertEqual(activity.render_summary({"headline": "", "actions": []}), "")


class ProposalParsingTest(unittest.TestCase):
    """The agent wraps its answer in commentary. Only real actions may reach the human."""

    def _parse(self, text):
        from trace.agent import runner
        return runner._parse_proposals(text)

    def test_prose_around_the_block_is_ignored(self):
        text = ("# Summary\nI checked three payment emails and cross-referenced the sheet.\n\n"
                "| Invoice | Status |\n|---|---|\n| INV-1 | unpaid |\n\n"
                "Now let me output the final proposed actions.\n\n" + _PROPOSAL)
        got = self._parse(text)
        self.assertEqual([p["summary"] for p in got],
                         ["Add ERP row for ORD-029", "Confirm order to Field Oak Bistro"])

    def test_the_prompts_own_example_is_never_staged(self):
        """Observed in production 2026-08-12: a freshly built agent staged
        "Confirm PO-1234 to buyer@acme.com ($4,200, ships Aug 15)" — the worked example
        out of its OWN system prompt, rationale and all — and it reached a human as an
        approvable outbound email. It read like another account's data leaking in; it was
        the model copying an illustration it had been told twice not to copy.

        The prompt says "if your output resembles the example, you have made a mistake
        and should output [] instead". Prose cannot be the only thing between a fictional
        example and a real send, so the parser enforces it.
        """
        echoed = ('```proposed_actions\n[{"app": "Gmail", "action": "send email", '
                  '"summary": "Confirm PO-1234 to buyer@acme.com ($4,200, ships Aug 15)", '
                  '"rationale": "Inbound email from buyer@acme.com on Aug 9 asked for '
                  'written confirmation of order PO-1234", '
                  '"details": {"to": "buyer@acme.com"}}]\n```')
        self.assertEqual(self._parse(echoed), [])

    def test_a_canary_anywhere_in_the_item_drops_it(self):
        """Only the details carry the canary here — a summary rewritten in the worker's
        own words must not smuggle the example's recipient through."""
        sneaky = ('```proposed_actions\n[{"app": "Gmail", "action": "send email", '
                  '"summary": "Confirm this week\'s order", '
                  '"details": {"to": "buyer@acme.com", "subject": "Confirmed"}}]\n```')
        self.assertEqual(self._parse(sneaky), [])

    def test_real_proposals_are_untouched_by_the_guard(self):
        from trace.agent import runner
        got = runner._parse_proposals(_PROPOSAL)
        self.assertEqual(len(got), 2)                  # the guard is not over-eager

    def test_narration_shaped_like_an_action_is_dropped(self):
        """A summary is one line a human approves at a glance; an essay in that field is
        the tell that the object is narration, not an action."""
        essay = "The lessons_learned.md file has already been updated by a concurrent " * 8
        text = ('```proposed_actions\n[{"summary": "' + essay + '", "details": "no new lessons"}]\n```')
        self.assertEqual(self._parse(text), [])

    def test_last_block_wins(self):
        """An earlier draft the agent talked itself out of must not be approvable."""
        first = ('```proposed_actions\n[{"app": "Gmail", "action": "send email", '
                 '"summary": "Draft I reconsidered", "details": {"to": "x@y.com"}}]\n```')
        text = first + "\n\nOn reflection that was wrong. Here are the real actions:\n\n" + _PROPOSAL
        got = self._parse(text)
        self.assertEqual([p["summary"] for p in got],
                         ["Add ERP row for ORD-029", "Confirm order to Field Oak Bistro"])

    def test_empty_array_is_a_valid_answer(self):
        self.assertEqual(self._parse('```proposed_actions\n[]\n```'), [])

    def test_no_block_at_all(self):
        self.assertIsNone(self._parse("I read the inbox and there was nothing to do."))


if __name__ == "__main__":
    unittest.main()
