"""The session profile (trace/agent/profile.py) — where a session's tokens went.

The engine reads a COMPACTED transcript, which truncates every tool result to 200
characters, so it cannot see that a scan returned 40KB or that the same call was made
four times with identical arguments. Those are exactly the facts a change gets built on,
so they are measured in code and handed over as numbers.
"""

import unittest

from trace.agent import profile


def _call(name, inp, result=""):
    return [{"type": "mcp_tool_use", "name": name, "input": inp},
            {"type": "tool_result", "text": result}]


def _turn(inp=1000, out=100):
    return {"type": "agent.message", "text": "working",
            "usage": {"input_tokens": inp, "output_tokens": out}}


class RepeatedCallsTest(unittest.TestCase):
    def test_identical_arguments_are_flagged_and_similar_ones_are_not(self):
        events = (_call("sheets.read", {"range": "A1:Z"}, "x" * 4000)
                  + _call("sheets.read", {"range": "A1:Z"}, "x" * 4000)
                  + _call("sheets.read", {"range": "B1:B9"}, "y" * 10))
        rep = profile.repeated_calls(profile.tool_calls(events))
        self.assertEqual(len(rep), 1)
        self.assertEqual((rep[0]["name"], rep[0]["count"]), ("sheets.read", 2))
        self.assertEqual(rep[0]["result_chars"], 4000)

    def test_key_order_does_not_hide_a_repeat(self):
        events = (_call("q", {"a": 1, "b": 2}) + _call("q", {"b": 2, "a": 1}))
        self.assertEqual(profile.repeated_calls(profile.tool_calls(events))[0]["count"], 2)

    def test_a_call_made_once_is_not_a_repeat(self):
        self.assertEqual(profile.repeated_calls(profile.tool_calls(_call("q", {}))), [])


class ReferencedDocsTest(unittest.TestCase):
    def test_a_document_read_through_the_mount_is_seen(self):
        events = _call("fs.read", {"path": "/mnt/memory/store-a/lessons_learned.md"},
                       "old lesson")
        self.assertEqual(profile.referenced_docs(events, ["/lessons_learned.md"]),
                         {"/lessons_learned.md"})

    def test_a_document_never_touched_is_not_seen(self):
        """This is what makes pruning proposable: a document read every session and never
        referenced is pure recurring cost."""
        events = _call("fs.read", {"path": "/mnt/memory/store-a/lessons_learned.md"})
        self.assertEqual(
            profile.referenced_docs(events, ["/lessons_learned.md", "/unused.md"]),
            {"/lessons_learned.md"})

    def test_nothing_read_references_nothing(self):
        self.assertEqual(profile.referenced_docs([_turn()], ["/a.md"]), set())


class PrefixTest(unittest.TestCase):
    def test_the_prefix_is_measured_from_the_first_turn_not_estimated(self):
        """The first turn's reported input IS the prefix: system prompt, tool schemas,
        memory and the trigger. Only the memory share is an estimate."""
        events = [_turn(inp=40000), _turn(inp=52000)]
        p = profile.prefix(events, {"/a.md": "z" * 8000})
        self.assertEqual(p["prefix_tokens_measured"], 40000)
        self.assertEqual(p["memory_store_tokens_estimated"], 2000)
        self.assertEqual(p["memory_share_of_prefix"], 0.05)

    def test_a_session_with_no_usage_reports_no_prefix_rather_than_a_guess(self):
        self.assertIsNone(profile.prefix([], {"/a.md": "z"})["prefix_tokens_measured"])
        self.assertIsNone(profile.prefix(None, {})["memory_share_of_prefix"])


class PreconditionVectorTest(unittest.TestCase):
    """The countable half. Prose cannot be compared against numbers, so this is what a
    fleet matcher actually filters on."""

    def test_documents_never_referenced_are_counted(self):
        prof = profile.session_profile(
            _call("fs.read", {"path": "/mnt/memory/s/used.md"}), ["/used.md", "/cold.md"])
        v = profile.precondition_vector([prof], {"/used.md": "a", "/cold.md": "b"})
        self.assertEqual(v["docs_total"], 2)
        self.assertEqual(v["docs_never_referenced"], 1)

    def test_short_sessions_are_a_share_not_a_verdict(self):
        long_one = profile.session_profile([_turn()] * 9, [])
        short_one = profile.session_profile([_turn()], [])
        v = profile.precondition_vector([long_one, short_one, short_one], {})
        self.assertEqual(v["short_session_share"], 0.67)
        self.assertEqual(v["median_turns"], 1)

    def test_an_empty_group_never_divides_by_zero(self):
        v = profile.precondition_vector([], {})
        self.assertEqual((v["short_session_share"], v["median_turns"]), (0.0, 0))
