"""The activity engine: summarize real session logs (injected model_fn) + extract
staged proposed actions + classify the session outcome. Offline, no platform."""

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
