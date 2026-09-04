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


def _session_level(turn_count, total_input=1_750_000):
    """The shape this platform actually sends: many turns, ONE usage payload carrying
    the session's whole total. Five production sessions looked exactly like this, and
    one of them reported a single turn spending 1.75 million tokens."""
    ev = [{"type": "user.message", "text": "go"}]
    for i in range(turn_count):
        ev += [{"type": "mcp_tool_use", "name": "q", "input": {"i": i}},
               {"type": "tool_result", "text": "x" * 100},
               {"type": "agent.message", "text": "step"}]
    ev[-1] = {"type": "agent.message", "text": "done",
              "usage": {"input_tokens": total_input, "output_tokens": 2315}}
    ev.append({"type": "session.status_idle", "text": ""})
    return ev


class TurnCountTest(unittest.TestCase):
    """Turns are counted from the agent's MESSAGES. Counting usage payloads was the
    first attempt: on this platform there is one per session, so every session read as a
    single turn -- including one spending 1.75 million tokens, which no context window
    would hold."""

    def test_a_session_level_usage_payload_does_not_read_as_one_turn(self):
        self.assertEqual(profile.turns(_session_level(16)), 16)

    def test_per_turn_usage_is_recognised_as_such(self):
        self.assertFalse(profile.per_turn_usage_reported(_session_level(16)))
        self.assertTrue(profile.per_turn_usage_reported([_turn(), _turn()]))

    def test_a_stream_with_no_agent_messages_falls_back_to_tool_results(self):
        events = _call("q", {}, "r") + _call("q", {"b": 1}, "r")
        self.assertEqual(profile.turns(events), 3)          # two results, plus the answer

    def test_a_session_that_ran_at_all_took_at_least_one_turn(self):
        self.assertEqual(profile.turns([]), 1)
        self.assertEqual(profile.turns([{"type": "user.message", "text": "go"}]), 1)


class PrefixTest(unittest.TestCase):
    def test_an_unmeasurable_prefix_is_None_and_says_why(self):
        """A number labelled "prefix" that is silently the whole session's input is worse
        than an absent one: it told the engine memory was a rounding error on every agent
        it would ever see."""
        p = profile.prefix(_session_level(16), {"/a.md": "z" * 40000})
        self.assertIsNone(p["prefix_tokens_measured"])
        self.assertIn("one usage total per session", p["note"])

    def test_a_real_per_turn_prefix_is_reported_when_it_exists(self):
        p = profile.prefix([_turn(inp=40000), _turn(inp=52000)], {"/a.md": "z" * 8000})
        self.assertEqual(p["prefix_tokens_measured"], 40000)

    def test_memory_is_counted_once_per_turn_because_it_is_re_sent_every_turn(self):
        """10K of memory over 16 turns is 160K of input, not 10K. Counting it once was
        what made memory look negligible."""
        p = profile.prefix(_session_level(16, total_input=1_750_000),
                           {"/a.md": "z" * 40000})
        self.assertEqual(p["turns"], 16)
        self.assertEqual(p["memory_store_tokens_estimated"], 10000)
        self.assertEqual(p["memory_reads_estimated"], 160000)
        self.assertEqual(p["memory_share_of_input"], 0.091)

    def test_tokens_per_turn_is_empty_rather_than_a_fabricated_single_row(self):
        self.assertEqual(profile.tokens_per_turn(_session_level(16)), [])
        self.assertEqual(len(profile.tokens_per_turn([_turn(), _turn()])), 2)

    def test_a_session_with_no_usage_at_all_reports_no_totals(self):
        p = profile.prefix([{"type": "agent.message", "text": "x"}], {"/a.md": "z"})
        self.assertIsNone(p["total_input_tokens"])
        self.assertIsNone(p["memory_share_of_input"])


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
