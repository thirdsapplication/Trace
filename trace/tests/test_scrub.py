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
    def test_live_state_never_travels(self):
        # An EXCLUSION, not a whitelist: whatever the engine has built for this agent
        # goes, because a document it worked out is exactly what a similar agent should
        # see. What never goes is the status file — live workflow state is the densest
        # concentration of current customer names and order references in the store, and
        # the least transferable thing in it.
        docs = {config.MEMORY_LESSONS_DOC: "lessons",
                "/playbook.md": "a document the engine invented",
                config.MEMORY_STATUS_DOC: "live state: order 8891 for a real customer"}
        payload = scrub.build_payload(_records(), docs)
        self.assertEqual(set(payload["memory"]),
                         {config.MEMORY_LESSONS_DOC, "/playbook.md"})
        self.assertNotIn("8891", json.dumps(payload))

    def test_unreadable_and_empty_docs_are_dropped(self):
        payload = scrub.build_payload(_records(), {config.MEMORY_LESSONS_DOC: None,
                                                   "/playbook.md": "   "})
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

    def test_a_bare_digit_run_is_NOT_caught_by_the_sweep(self):
        """The tradeoff, stated so it is visible rather than assumed.

        An undeclared order or invoice number no longer fails the sweep. It cannot: the
        rule that caught it ("four or more digits") also caught every year, token count,
        row range and timestamp, and rejected seven production batches out of seven. Such
        an id is instead the scrubber's job to replace — the prompt names order and record
        ids explicitly — the declared-literal check's job to verify once it has been, and
        the independent model gate's job to catch when it has not. This test exists so
        that anyone reading the suite knows the deterministic layer does NOT cover it."""
        ok, _ = scrub.leak_sweep(_scrubbed("[agent] order 88213 shipped"), [])
        self.assertTrue(ok)

    def test_a_declared_order_number_that_survives_IS_caught(self):
        # The half that does still hold deterministically: once the scrubber says it
        # replaced an id, a surviving copy is a leak the code finds without any model.
        scrubbed = _scrubbed("[agent] order 88213 shipped for restaurant client A")
        red = [{"pseudonym": "restaurant client A", "literal": "88213",
                "category": "id"}]
        ok, reason = scrub.leak_sweep(scrubbed, red)
        self.assertFalse(ok, reason)

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


class ChecksDisabledTest(unittest.TestCase):
    """TRACE_SCRUB_CHECKS=0 — the debugging switch that separates "nothing qualified"
    from "this is broken", which every rejection log otherwise renders identically."""

    def _run(self, scrub_reply, verify_reply='{"safe": false}'):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = scrub.scrub_and_verify(_records(), {}, _fn(scrub_reply),
                                         _fn(verify_reply))
        return out, buf.getvalue()

    def setUp(self):
        self._orig = config.SCRUB_CHECKS_ENABLED
        config.SCRUB_CHECKS_ENABLED = False
        self.addCleanup(setattr, config, "SCRUB_CHECKS_ENABLED", self._orig)

    def test_a_batch_the_privacy_gate_refuses_still_publishes(self):
        out, _ = self._run(json.dumps(_scrubbed()))
        self.assertIsNotNone(out)

    def test_a_summarised_scrub_still_publishes(self):
        payload = _scrubbed()
        payload["sessions"][0]["transcript"] = "the agent did some things"
        out, _ = self._run(json.dumps(payload))
        self.assertIsNotNone(out, "the structural check must be bypassed too")

    def test_a_leaky_scrub_still_publishes(self):
        payload = _scrubbed("[agent] emailed chef@mariospizzeria.example")
        out, _ = self._run(json.dumps(payload))
        self.assertIsNotNone(out, "the leak sweep must be bypassed too")

    def test_every_bypassed_publish_warns(self):
        # Once at startup would not do: a flag set for one run and forgotten is exactly
        # how unverified material gets shared, and the log is the only place it shows.
        _, log = self._run(json.dumps(_scrubbed()))
        self.assertIn("CHECKS DISABLED", log)
        self.assertIn("TRACE_SCRUB_CHECKS=0", log)

    def test_a_failed_scrub_call_still_publishes_nothing(self):
        # The switch bypasses the CHECKS, not the scrub. With no scrubbed text there is
        # nothing to publish under any flag.
        out, _ = self._run("not json at all")
        self.assertIsNone(out)

    def test_redactions_are_still_stripped(self):
        out, _ = self._run(json.dumps(_scrubbed(
            redactions=[{"pseudonym": "restaurant client A",
                         "literal": "Mario's Pizzeria", "category": "company"}])))
        self.assertNotIn("redactions", out)
        self.assertNotIn("Mario", json.dumps(out))


class VerifyGateDiagnosticsTest(unittest.TestCase):
    """"did not answer safe" covered two different things: the gate judged the content
    unsafe, or the gate never answered. Conflating them cost a production round when
    max_tokens=100 on a model that thinks by default returned an empty string."""

    def _log(self, verify_reply):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scrub.verify_scrub(_scrubbed(), _fn(verify_reply))
        return buf.getvalue()

    def test_an_empty_reply_is_reported_as_a_broken_call(self):
        log = self._log("")
        self.assertIn("returned NOTHING", log)
        self.assertIn("broken call, not a judgement", log)

    def test_a_non_json_reply_is_reported_as_such(self):
        self.assertIn("did not answer JSON", self._log("I cannot help with that"))

    def test_a_genuine_refusal_is_reported_as_a_judgement(self):
        self.assertIn("judged the batch unsafe", self._log('{"safe": false}'))

    def test_a_safe_answer_still_passes(self):
        self.assertTrue(scrub.verify_scrub(_scrubbed(), _fn('{"safe": true}')))


class LeakSweepPrecisionTest(unittest.TestCase):
    """The sweep must not fire on ordinary transcript furniture.

    Every one of these is drawn from what a real compacted transcript contains, and every
    one of them was REJECTED by the first version of this sweep, which failed any run of
    four or more digits. In production that rejected seven batches out of seven: nothing
    was ever published, the peer table stayed empty, and cross-agent learning read as a
    similarity problem. These cases are the regression guard on that."""

    ORDINARY = {
        "iso timestamp": '[tool result] {"date": "2026-08-28T19:03:41Z"}',
        "millisecond epoch": '[tool result] {"internalDate": "1756400621000"}',
        "message id": '[tool call] gmail.get {"id": "19abc4d5e6f78901"}',
        "a year in prose": "[agent] no invoices dated 2026 were outstanding",
        "spreadsheet range": '[tool call] sheets.read {"range": "A2:D1500"}',
        "a token count": "[end] completed after 41200 tokens",
        "an order reference": "[agent] recorded order 100482 as handled",
        "a clock time": "[agent] the digest is due at 09:30 each morning",
        "an http status": "[tool result (error)] 429 after 15000 ms, retried once",
    }

    IDENTIFIERS = {
        "email address": "[agent] emailed chef@mariospizzeria.example",
        "grouped card": "[tool result] card 4111 1111 1111 1111 on file",
        "bare card": "[tool result] 4111111111111111",
        "international phone": "[agent] called +1 (555) 123-4567 twice",
        "dashed phone": "[agent] contact 555-123-4567",
        "iban": "[tool result] GB29NWBK60161331926819",
        "url with a token": "[tool call] fetch https://x.example/share?token=abc123",
    }

    def _sweep(self, text):
        scrubbed = {"sessions": [{"session_id": "s1", "transcript": text}], "memory": {}}
        return scrub.leak_sweep(scrubbed, [])

    def test_ordinary_transcript_content_is_not_a_leak(self):
        for label, text in self.ORDINARY.items():
            with self.subTest(label):
                ok, reason = self._sweep(text)
                self.assertTrue(ok, f"{label} was rejected as {reason!r}")

    def test_real_identifiers_are_still_caught(self):
        for label, text in self.IDENTIFIERS.items():
            with self.subTest(label):
                ok, _ = self._sweep(text)
                self.assertFalse(ok, f"{label} was not caught")

    def test_a_whole_realistic_transcript_passes(self):
        # The shape that matters: many of the ordinary cases at once, as one batch.
        ok, reason = self._sweep("\n".join(self.ORDINARY.values()))
        self.assertTrue(ok, f"a realistic transcript was rejected as {reason!r}")

    def test_one_identifier_among_ordinary_content_still_fails_the_batch(self):
        text = "\n".join(list(self.ORDINARY.values())
                          + ["[agent] emailed chef@mariospizzeria.example"])
        ok, _ = self._sweep(text)
        self.assertFalse(ok, "an email hiding among ordinary content was missed")


class ScrubReplyDiagnosticsTest(unittest.TestCase):
    """An unparseable reply used to log "produced nothing", which reads identically to a
    call that never happened — and hid four consecutive failures in production."""

    def test_an_unparseable_reply_reports_its_size_and_tail(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = scrub.scrub_batch(_records(), {},
                                    _fn("here is your scrubbed text, no JSON at all"))
        self.assertIsNone(out)
        log = buf.getvalue()
        self.assertIn("did not parse as JSON", log)
        self.assertIn("chars out", log)
        self.assertIn("no JSON at all", log)          # the tail, for diagnosis

    def test_a_reply_stopping_mid_structure_says_so(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scrub.scrub_batch(_records(), {},
                              _fn('{"sessions": [{"transcript": "half a docum'))
        self.assertIn("ends mid-structure", buf.getvalue())


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
        # An email survives the rewrite: a shape the sweep still catches on its own,
        # before the paid gate is asked anything.
        leaky = _scrubbed(SCRUBBED_TRANSCRIPT.replace("ok", "ok, chef@mario.example"))
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

    def test_live_state_invented_into_the_answer_is_dropped(self):
        payload = _scrubbed()
        payload["memory"][config.MEMORY_STATUS_DOC] = "live state"
        out = scrub.scrub_and_verify(_records(), {}, _fn(json.dumps(payload)),
                                     _fn('{"safe": true}'))
        self.assertEqual(set(out["memory"]), {config.MEMORY_LESSONS_DOC})


if __name__ == "__main__":
    unittest.main()
