"""Privacy pre-call (trace/agent/scrub.py) — offline, stub model_fns.

The checks matter more than the happy path, because the model's answer is untrusted and
the cost of accepting a bad one is one customer's material inside another customer's
learning payload. So: a summarised scrub is caught by line counts, an identifier the model
never declared is caught by the unconditional sweep, the re-identification key never
survives into the returned payload, and every unclear answer from the privacy gate is a
rejection.
"""

import json
import unittest

from trace.agent import config, scrub

TRANSCRIPT = ("[trigger] schedule\n"
              "[agent] emailing Mario's Pizzeria about the late delivery\n"
              "[tool call] gmail.send\n"
              "[tool result] ok\n"
              "[end] completed")

SCRUBBED_TRANSCRIPT = ("[trigger] schedule\n"
                       "[agent] emailing restaurant client A about the late delivery\n"
                       "[tool call] gmail.send\n"
                       "[tool result] ok\n"
                       "[end] completed")


def _records(transcript=TRANSCRIPT):
    return [{"session_id": "s1", "ran_at": "2026-05-01T10:00:00Z", "status": "completed",
             "tokens": {"input_tokens": 4200, "output_tokens": 310, "total_tokens": 4510,
                        "estimated": False},
             "transcript": transcript}]


def _scrubbed(transcript=SCRUBBED_TRANSCRIPT, redactions=None):
    return {"sessions": [{"session_id": "s1", "ran_at": "2026-05-01T10:00:00Z",
                          "status": "completed",
                          "tokens": {"total_tokens": 4510},
                          "transcript": transcript}],
            "memory": {"/lessons_learned.md": "Check the client's order state first."},
            "redactions": [] if redactions is None else redactions}


def _fn(answer):
    """A model_fn that ignores its inputs and returns a fixed string."""
    return lambda system, user: answer


def _raiser(exc=RuntimeError("boom")):
    def call(system, user):
        raise exc
    return call


class BuildPayloadTest(unittest.TestCase):
    def test_only_whitelisted_docs_travel(self):
        docs = {config.MEMORY_LESSONS_DOC: "lessons",
                config.MEMORY_STATUS_DOC: "status",
                config.MEMORY_SHARED_DOC: "fleet lessons from other agents"}
        payload = scrub.build_payload(_records(), docs)
        self.assertEqual(set(payload["memory"]),
                         {config.MEMORY_LESSONS_DOC, config.MEMORY_STATUS_DOC})

    def test_unreadable_and_empty_docs_are_dropped(self):
        payload = scrub.build_payload(_records(), {config.MEMORY_LESSONS_DOC: None,
                                                   config.MEMORY_STATUS_DOC: "   "})
        self.assertEqual(payload["memory"], {})

    def test_transcript_and_doc_are_clipped(self):
        recs = _records("x" * (config.SCRUB_SESSION_CLIP + 500))
        payload = scrub.build_payload(recs, {config.MEMORY_LESSONS_DOC:
                                             "y" * (config.SCRUB_DOC_CLIP + 500)})
        self.assertEqual(len(payload["sessions"][0]["transcript"]),
                         config.SCRUB_SESSION_CLIP)
        self.assertEqual(len(payload["memory"][config.MEMORY_LESSONS_DOC]),
                         config.SCRUB_DOC_CLIP)

    def test_session_fields_are_carried_through(self):
        s = scrub.build_payload(_records(), {})["sessions"][0]
        self.assertEqual(s["session_id"], "s1")
        self.assertEqual(s["status"], "completed")
        self.assertEqual(s["tokens"]["total_tokens"], 4510)


class StructuralCheckTest(unittest.TestCase):
    def test_line_for_line_scrub_passes(self):
        ok, reason = scrub.structural_check(_records(), _scrubbed())
        self.assertTrue(ok, reason)

    def test_summary_is_caught_by_line_count(self):
        ok, reason = scrub.structural_check(
            _records(), _scrubbed("[agent] emailed a restaurant client"))
        self.assertFalse(ok)
        self.assertIn("lines", reason)

    def test_dropped_session_is_caught(self):
        payload = _scrubbed()
        payload["sessions"] = []
        ok, reason = scrub.structural_check(_records(), payload)
        self.assertFalse(ok)
        self.assertIn("count", reason)

    def test_renamed_session_id_is_caught(self):
        payload = _scrubbed()
        payload["sessions"][0]["session_id"] = "session A"
        ok, reason = scrub.structural_check(_records(), payload)
        self.assertFalse(ok)
        self.assertIn("unknown session id", reason)

    def test_missing_sessions_key_is_caught(self):
        ok, _ = scrub.structural_check(_records(), {"memory": {}})
        self.assertFalse(ok)

    def test_line_count_compares_against_the_clipped_original(self):
        # The model only ever saw the clipped copy, so an over-long record must not fail
        # merely for having lines the scrub was never shown.
        long_lines = "\n".join(["[agent] step"] * 4000)
        clipped = long_lines[:config.SCRUB_SESSION_CLIP]
        ok, reason = scrub.structural_check(_records(long_lines), _scrubbed(clipped))
        self.assertTrue(ok, reason)


class LeakSweepTest(unittest.TestCase):
    def test_clean_payload_passes(self):
        ok, reason = scrub.leak_sweep(_scrubbed(), [])
        self.assertTrue(ok, reason)

    def test_structural_fields_do_not_trip_the_sweep(self):
        # ran_at ("2026-...") and a five-figure token total are 4+ digit runs the scrub is
        # required to preserve; sweeping them would reject every payload ever produced.
        payload = _scrubbed()
        payload["sessions"][0]["tokens"] = {"total_tokens": 41999}
        ok, reason = scrub.leak_sweep(payload, [])
        self.assertTrue(ok, reason)

    def test_declared_literal_still_present_fails(self):
        red = [{"pseudonym": "restaurant client A", "literal": "Mario's Pizzeria",
                "category": "company"}]
        ok, reason = scrub.leak_sweep(_scrubbed(TRANSCRIPT, red), red)
        self.assertFalse(ok)
        self.assertIn("literal still present", reason)

    def test_literal_match_is_case_insensitive(self):
        red = [{"pseudonym": "restaurant client A", "literal": "MARIO'S PIZZERIA"}]
        ok, _ = scrub.leak_sweep(_scrubbed(TRANSCRIPT, red), red)
        self.assertFalse(ok)

    def test_reason_never_quotes_the_literal(self):
        red = [{"pseudonym": "restaurant client A", "literal": "Mario's Pizzeria"}]
        _, reason = scrub.leak_sweep(_scrubbed(TRANSCRIPT, red), red)
        self.assertNotIn("Mario", reason)

    def test_declared_literal_in_the_redactions_list_is_not_a_leak(self):
        # Regression: the redactions list quotes every literal by definition, so folding
        # it into the searched text rejected every CORRECT scrub.
        red = [{"pseudonym": "restaurant client A", "literal": "Mario's Pizzeria"}]
        ok, reason = scrub.leak_sweep(_scrubbed(redactions=red), red)
        self.assertTrue(ok, reason)

    def test_declared_pseudonym_never_used_fails(self):
        red = [{"pseudonym": "law firm B", "literal": "Winters & Cole"}]
        ok, reason = scrub.leak_sweep(_scrubbed(), red)
        self.assertFalse(ok)
        self.assertIn("never used", reason)

    def test_empty_literal_is_skipped(self):
        ok, reason = scrub.leak_sweep(_scrubbed(), [{"pseudonym": "x", "literal": "   "}])
        self.assertTrue(ok, reason)

    def test_pseudonym_without_literal_declaration_fails(self):
        ok, _ = scrub.leak_sweep(_scrubbed(), [{"pseudonym": "", "literal": "Acme Inc"}])
        self.assertFalse(ok)

    def test_undeclared_email_fails(self):
        ok, reason = scrub.leak_sweep(
            _scrubbed("[agent] wrote to dana@example.com\n[end] done"), [])
        self.assertFalse(ok)
        self.assertIn("email", reason)

    def test_undeclared_phone_fails(self):
        # Asserting the REASON, not just the verdict: a digit run trips several rules at
        # once, and a phone that only fails via the generic 4+ digit rule would hide a
        # broken phone pattern behind a passing test.
        ok, reason = scrub.leak_sweep(_scrubbed("[agent] called +1 (555) 019-2231"), [])
        self.assertFalse(ok)
        self.assertIn("phone", reason)

    def test_undeclared_digit_run_fails(self):
        ok, reason = scrub.leak_sweep(_scrubbed("[agent] order 88213 shipped"), [])
        self.assertFalse(ok)
        self.assertIn("digit run", reason)

    def test_url_with_query_string_fails(self):
        ok, reason = scrub.leak_sweep(
            _scrubbed("[tool call] fetch https://crm.example.com/c?id=ab"), [])
        self.assertFalse(ok)
        self.assertIn("query string", reason)

    def test_card_shaped_string_fails(self):
        ok, _ = scrub.leak_sweep(_scrubbed("[agent] card 4111-1111-1111-1111 on file"), [])
        self.assertFalse(ok)

    def test_leak_in_memory_doc_is_caught(self):
        payload = _scrubbed()
        payload["memory"]["/lessons_learned.md"] = "Escalate to dana@example.com."
        ok, _ = scrub.leak_sweep(payload, [])
        self.assertFalse(ok)


class VerifyScrubTest(unittest.TestCase):
    def test_safe_true_passes(self):
        self.assertTrue(scrub.verify_scrub(_scrubbed(), _fn('{"safe": true}')))

    def test_safe_false_fails(self):
        self.assertFalse(scrub.verify_scrub(_scrubbed(), _fn('{"safe": false}')))

    def test_empty_payload_fails_closed(self):
        self.assertFalse(scrub.verify_scrub({}, _fn('{"safe": true}')))
        self.assertFalse(scrub.verify_scrub(None, _fn('{"safe": true}')))

    def test_unparseable_answer_fails_closed(self):
        self.assertFalse(scrub.verify_scrub(_scrubbed(), _fn("sure, looks fine")))

    def test_empty_model_output_fails_closed(self):
        # A truncated or refused reply parses to {}, which is not an answer — and the
        # default for "no answer" has to be no.
        self.assertFalse(scrub.verify_scrub(_scrubbed(), _fn("")))
        self.assertFalse(scrub.verify_scrub(_scrubbed(), _fn(None)))

    def test_truthy_but_not_true_fails_closed(self):
        self.assertFalse(scrub.verify_scrub(_scrubbed(), _fn('{"safe": "yes"}')))

    def test_exception_fails_closed(self):
        self.assertFalse(scrub.verify_scrub(_scrubbed(), _raiser()))

    def test_gate_never_sees_the_redaction_literals(self):
        seen = {}

        def call(system, user):
            seen["user"] = user
            return '{"safe": true}'

        red = [{"pseudonym": "restaurant client A", "literal": "Mario's Pizzeria"}]
        scrub.verify_scrub(_scrubbed(redactions=red), call)
        self.assertNotIn("Mario", seen["user"])
        self.assertNotIn("redactions", seen["user"])


class ScrubBatchTest(unittest.TestCase):
    def test_parsed_answer_is_returned(self):
        out = scrub.scrub_batch(_records(), {}, _fn(json.dumps(_scrubbed())))
        self.assertEqual(out["sessions"][0]["session_id"], "s1")

    def test_no_sessions_is_none(self):
        self.assertIsNone(scrub.scrub_batch([], {}, _fn(json.dumps(_scrubbed()))))

    def test_call_failure_is_none_not_raise(self):
        self.assertIsNone(scrub.scrub_batch(_records(), {}, _raiser()))

    def test_unparseable_answer_is_none(self):
        self.assertIsNone(scrub.scrub_batch(_records(), {}, _fn("no json here")))


class LeakSweepEscapingTest(unittest.TestCase):
    """The declared-literal check must compare against the TEXT, not a serialisation of
    it. json.dumps escapes a double quote, a backslash and every control character, so a
    literal containing one would survive verbatim in what gets published and still never
    match — silently disarming the one check here that does not take the model's word for
    anything.

    Each case models a PARTIAL scrub: the pseudonym is present (so the
    declared-but-never-used check passes and cannot mask the result) and one occurrence
    of the original was missed. Only the literal check can catch that.
    """

    PSEUDO = "restaurant client A"

    def _sweep(self, literal, leaked_text):
        scrubbed = {"sessions": [{"session_id": "s1",
                                  "transcript": f"[agent] chased {self.PSEUDO}\n"
                                                f"[agent] {leaked_text}"}],
                    "memory": {}}
        red = [{"pseudonym": self.PSEUDO, "literal": literal, "category": "company"}]
        return scrub.leak_sweep(scrubbed, red)

    def test_a_clean_partial_free_payload_passes(self):
        # Control: same shape, nothing leaked. Without this the cases below could pass
        # for a reason that has nothing to do with escaping.
        ok, reason = self._sweep("Mario's Pizzeria", "invoice sent")
        self.assertTrue(ok, reason)

    def test_literal_with_a_double_quote_is_caught(self):
        ok, reason = self._sweep('the "Blue Door" cafe',
                                 'emailed the "Blue Door" cafe')
        self.assertFalse(ok, "a literal containing a double quote escaped the sweep")

    def test_literal_with_a_backslash_is_caught(self):
        ok, reason = self._sweep("CORP\\jsmith", "authenticated as CORP\\jsmith")
        self.assertFalse(ok, "a literal containing a backslash escaped the sweep")

    def test_literal_spanning_a_newline_is_caught(self):
        ok, reason = self._sweep("Mario\nPizzeria", "signed off Mario\nPizzeria")
        self.assertFalse(ok, "a literal spanning a newline escaped the sweep")

    def test_literal_with_a_tab_is_caught(self):
        ok, reason = self._sweep("Mario\tPizzeria", "column read Mario\tPizzeria")
        self.assertFalse(ok, "a literal containing a tab escaped the sweep")

    def test_literal_hidden_in_a_key_is_caught(self):
        # A model that answered {"<real name>": "..."} put the literal in a key, which a
        # values-only walk would never see.
        scrubbed = {"sessions": [{"session_id": "s1",
                                  "transcript": f"[agent] chased {self.PSEUDO}"}],
                    "memory": {'the "Blue Door" cafe': "notes"}}
        red = [{"pseudonym": self.PSEUDO, "literal": 'the "Blue Door" cafe',
                "category": "company"}]
        ok, _ = scrub.leak_sweep(scrubbed, red)
        self.assertFalse(ok, "a literal hiding in a dict key escaped the sweep")


class ScrubAndVerifyTest(unittest.TestCase):
    def test_a_well_formed_scrub_round_trips_sessions_and_memory(self):
        red = [{"pseudonym": "restaurant client A", "literal": "Mario's Pizzeria",
                "category": "company"}]
        answer = json.dumps(_scrubbed(redactions=red))
        out = scrub.scrub_and_verify(_records(), {}, _fn(answer), _fn('{"safe": true}'))
        self.assertIsNotNone(out)
        self.assertEqual(out["sessions"][0]["index"], 1)
        self.assertEqual(out["sessions"][0]["transcript"], SCRUBBED_TRANSCRIPT)
        self.assertEqual(out["memory"][config.MEMORY_LESSONS_DOC],
                         "Check the client's order state first.")
        self.assertNotIn("Mario", json.dumps(out))

    def test_the_redaction_key_never_reaches_the_caller(self):
        # redactions is pseudonym -> the ORIGINAL literal: the one artifact that can UNDO
        # the scrub. The checks consume it and it is dropped here, so nothing downstream
        # can persist or log a re-identification key alongside the material it unlocks.
        red = [{"pseudonym": "restaurant client A", "literal": "Mario's Pizzeria",
                "category": "company"}]
        out = scrub.scrub_and_verify(_records(), {},
                                     _fn(json.dumps(_scrubbed(redactions=red))),
                                     _fn('{"safe": true}'))
        self.assertNotIn("redactions", out)
        self.assertEqual(set(out), {"sessions", "memory"})

    def test_a_pseudonym_reused_across_sessions_survives_verification(self):
        # Stable pseudonyms are why the batch is scrubbed as ONE payload: the
        # cross-session pattern a peer reads for is only visible when the same real
        # entity reads as the same entity in session 1 and session 2. Reusing a
        # pseudonym must therefore pass the sweep, not read as a repeated leak.
        records = [
            {"session_id": "s1", "ran_at": "2026-05-01T10:00:00Z", "status": "completed",
             "tokens": {"total_tokens": 4510},
             "transcript": "[agent] chasing Mario's Pizzeria\n[end] completed"},
            {"session_id": "s2", "ran_at": "2026-05-02T10:00:00Z", "status": "completed",
             "tokens": {"total_tokens": 3900},
             "transcript": "[agent] Mario's Pizzeria replied\n[end] completed"},
        ]
        answer = json.dumps({
            "sessions": [
                {"session_id": "s1",
                 "transcript": "[agent] chasing restaurant client A\n[end] completed"},
                {"session_id": "s2",
                 "transcript": "[agent] restaurant client A replied\n[end] completed"}],
            "memory": {config.MEMORY_LESSONS_DOC:
                       "restaurant client A answers on the second chase."},
            "redactions": [{"pseudonym": "restaurant client A",
                            "literal": "Mario's Pizzeria", "category": "company"}]})
        out = scrub.scrub_and_verify(records, {}, _fn(answer), _fn('{"safe": true}'))
        self.assertIsNotNone(out)
        self.assertEqual([s["index"] for s in out["sessions"]], [1, 2])
        self.assertEqual(sum(s["transcript"].count("restaurant client A")
                             for s in out["sessions"]), 2)
        self.assertNotIn("Mario", json.dumps(out))

    def test_summarised_scrub_is_rejected_before_the_paid_gate(self):
        calls = []

        def gate(system, user):
            calls.append(user)
            return '{"safe": true}'

        short = _scrubbed("[agent] emailed a restaurant client")
        out = scrub.scrub_and_verify(_records(), {}, _fn(json.dumps(short)), gate)
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_leaked_identifier_is_rejected(self):
        leaky = _scrubbed(SCRUBBED_TRANSCRIPT.replace("ok", "ok, ref 90210221"))
        out = scrub.scrub_and_verify(_records(), {}, _fn(json.dumps(leaky)),
                                     _fn('{"safe": true}'))
        self.assertIsNone(out)

    def test_gate_rejection_is_final(self):
        out = scrub.scrub_and_verify(_records(), {}, _fn(json.dumps(_scrubbed())),
                                     _fn('{"safe": false}'))
        self.assertIsNone(out)

    def test_failed_scrub_call_is_none_and_never_raises(self):
        self.assertIsNone(scrub.scrub_and_verify(_records(), {}, _raiser(),
                                                 _fn('{"safe": true}')))
        self.assertIsNone(scrub.scrub_and_verify(_records(), {}, _fn("not json"),
                                                 _fn('{"safe": true}')))
        # A gate that blows up is a gate that did not say safe.
        self.assertIsNone(scrub.scrub_and_verify(_records(), {},
                                                 _fn(json.dumps(_scrubbed())), _raiser()))

    def test_invented_session_field_never_reaches_the_payload(self):
        # An extra key is never swept — nothing downstream looks at a key the contract
        # does not name — so it must not survive into what gets published.
        payload = _scrubbed()
        payload["sessions"][0]["notes"] = "billed to Mario's Pizzeria"
        answer = json.dumps(payload)
        out = scrub.scrub_and_verify(_records(), {}, _fn(answer), _fn('{"safe": true}'))
        self.assertEqual(set(out["sessions"][0]),
                         {"index", "status", "tokens", "transcript"})
        self.assertNotIn("Mario", json.dumps(out))

    def test_platform_session_id_and_ran_at_never_reach_a_bundle(self):
        # A bundle is read by a DIFFERENT customer's agent. A platform session id names
        # the producing customer's run to anyone who can see the platform, and ran_at
        # discloses when that business operates. Both are required INPUTS (structural_check
        # matches sessions by id) and neither may be published, so this is the one place
        # the distinction can be enforced.
        red = [{"pseudonym": "restaurant client A", "literal": "Mario's Pizzeria",
                "category": "company"}]
        answer = json.dumps(_scrubbed(redactions=red))
        out = scrub.scrub_and_verify(_records(), {}, _fn(answer), _fn('{"safe": true}'))
        blob = json.dumps(out)
        self.assertNotIn("s1", [s.get("session_id") for s in out["sessions"]])
        self.assertNotIn("session_id", blob)
        self.assertNotIn("ran_at", blob)
        self.assertNotIn("2026-05-01T10:00:00Z", blob)

    def test_metadata_comes_from_the_record_not_the_answer(self):
        payload = _scrubbed()
        payload["sessions"][0]["tokens"] = {"total_tokens": 1}
        payload["sessions"][0]["status"] = "failed"
        answer = json.dumps(payload)
        out = scrub.scrub_and_verify(_records(), {}, _fn(answer), _fn('{"safe": true}'))
        self.assertEqual(out["sessions"][0]["status"], "completed")
        self.assertEqual(out["sessions"][0]["tokens"]["total_tokens"], 4510)

    def test_non_whitelisted_doc_in_the_answer_is_dropped(self):
        payload = _scrubbed()
        payload["memory"][config.MEMORY_SHARED_DOC] = "fleet lessons"
        out = scrub.scrub_and_verify(_records(), {}, _fn(json.dumps(payload)),
                                     _fn('{"safe": true}'))
        self.assertEqual(set(out["memory"]), {config.MEMORY_LESSONS_DOC})


if __name__ == "__main__":
    unittest.main()
