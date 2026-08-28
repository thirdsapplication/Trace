"""Peer selection (trace/agent/peers.py) — pure computation, offline, no model call.

The arithmetic is the least interesting thing under test. What these cases pin is the
rule that decides whether one customer's material may be put in front of another
customer's agent: TOOL OVERLAP GATES, COSINE ONLY TIERS. Every way that rule can rot is
a way a stranger's transcripts reach an agent that has nothing to learn from them, so
the two fixtures that matter most are the ones where the two signals disagree.

The four agents are the design's worked example, pinned verbatim so a later tweak to a
threshold or to the rank formula has to argue with them: A chases invoices; B sends
payment reminders on nearly the same tools and reads like the same job; C posts to
social media, shares one generic tool, and still embeds close to A because both
descriptions are polite paragraphs about handling incoming work; D processes expense
claims — a different business on identical machinery. C is the case the gate exists
for, D is the case the tier exists for, and a change that admits C or drops D has
broken the rule whatever its numbers say.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from trace.agent import config, peers

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

# --- the worked example ------------------------------------------------------------
# A: invoice chaser.
A_TOOLS = {"gmail.search": 42, "gmail.send": 11, "sheets.read": 28,
           "sheets.append": 9, "stripe.list_invoices": 17}
# B: payment reminders. Four of A's five, quickbooks where A has sheets.append.
B_TOOLS = {"gmail.search": 33, "gmail.send": 25, "sheets.read": 14,
           "stripe.list_invoices": 8, "quickbooks.get_bill": 6}
# C: social poster. One tool in common, and it is the generic one.
C_TOOLS = {"sheets.read": 21, "buffer.schedule": 40}
# D: expense processor. A's four gmail/sheets tools, expensify where A has stripe.
D_TOOLS = {"gmail.search": 19, "gmail.send": 7, "sheets.read": 31,
           "sheets.append": 22, "expensify.create_report": 13}

# Three toy dimensions stand in for the real profile embedding. Only the ANGLE between
# two vectors ever reaches classify, so hand-sized vectors exercise exactly the same
# behaviour as 1024 real ones while leaving the expected cosine checkable by eye: with A
# lying on the first axis, cos(A, X) is just X's first component over X's length.
VEC_A = [1.00, 0.00, 0.00]
VEC_B = [0.96, 0.20, 0.20]      # ~0.96 — reads as the same job as A, and is
VEC_C = [0.88, 0.45, 0.15]      # ~0.88 — reads as the same job as A, and is not
VEC_D = [0.25, 0.15, 0.95]      # ~0.25 — a different job on the same machinery

_UNSET = object()


def _fp(tools):
    return {"tools": dict(tools), "sessions": 12, "avg_tokens": 9000}


A_FP = _fp(A_TOOLS)


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _bundle(agent_id, tools, embedding=None, days_ago=1, verified=True,
            created_at=_UNSET, owner=None, sessions=None):
    """One peer bundle in the shape ``registry.peer_bundles_latest`` returns."""
    return {"agent_id": agent_id,
            "owner_user_id": owner or f"usr-of-{agent_id}",
            "verified": verified,
            "created_at": _iso(days_ago) if created_at is _UNSET else created_at,
            "fingerprint": _fp(tools),
            "embedding": embedding,
            "sessions": sessions if sessions is not None else [
                {"session_id": "sess-1", "status": "completed",
                 "transcript": "read the sheet, then sent one chaser per overdue row"}],
            "memory": {config.MEMORY_LESSONS_DOC: "Batch the searches, one per run."}}


def _entry(session_id, transcript):
    """One ``select_peers`` result, sized by its transcript — the budget tests need
    peers of known cost in a chosen order, which selection would not let them pick."""
    return {"bundle": _bundle("agt-" + session_id, B_TOOLS, VEC_B,
                              sessions=[{"session_id": session_id,
                                         "transcript": transcript}]),
            "tier": "same_job", "overlap": 4 / 6,
            "shared_tools": ["gmail.search"], "rank": 0.9}


def _cost(entry):
    """What one entry costs against ``budget_chars`` — measured the way peer_payloads
    measures it, on the serialized payload rather than on the bundle."""
    return len(json.dumps(peers.peer_payloads([entry])[0], ensure_ascii=False,
                          default=str))


class WorkedExampleTest(unittest.TestCase):
    """classify() on the four fixture agents, one case per row of the design table."""

    def test_b_shares_four_tools_and_the_job_so_it_is_a_same_job_peer(self):
        v = peers.classify(A_FP, _fp(B_TOOLS), VEC_A, VEC_B)
        self.assertTrue(v["included"])
        self.assertEqual(v["tier"], "same_job")
        self.assertEqual(round(v["overlap"], 2), 0.67)          # 4 shared / 6 union
        self.assertGreaterEqual(v["cosine"], config.PEER_SAME_JOB_COSINE)

    def test_c_is_excluded_although_its_cosine_would_have_called_it_the_same_job(self):
        # The load-bearing case: two agents whose descriptions both read as "handle the
        # incoming stuff politely" embed close together while sharing one generic tool,
        # and nothing either learned about its own integrations helps the other. If this
        # ever passes the gate, prose similarity has become an admission criterion.
        v = peers.classify(A_FP, _fp(C_TOOLS), VEC_A, VEC_C)
        self.assertGreaterEqual(v["cosine"], config.PEER_SAME_JOB_COSINE)
        self.assertEqual(round(v["overlap"], 2), 0.17)          # 1 shared / 6 union
        self.assertLess(v["overlap"], config.PEER_MIN_TOOL_OVERLAP)
        self.assertFalse(v["included"])
        self.assertIsNone(v["tier"])

    def test_d_is_included_although_its_job_is_a_different_one(self):
        # The mirror case: "check whether the sheet changed before re-reading it" is a
        # property of sheets.read, not of invoices, so identical machinery earns a place
        # even when the businesses have nothing to do with each other.
        v = peers.classify(A_FP, _fp(D_TOOLS), VEC_A, VEC_D)
        self.assertTrue(v["included"])
        self.assertEqual(v["tier"], "same_tooling")
        self.assertEqual(round(v["overlap"], 2), 0.67)
        self.assertLess(v["cosine"], config.PEER_SAME_JOB_COSINE)

    def test_selection_keeps_b_and_d_in_rank_order_and_drops_c(self):
        selected = peers.select_peers(
            "agt-a-invoice-chaser", A_FP, VEC_A,
            [_bundle("agt-c-social-poster", C_TOOLS, VEC_C),
             _bundle("agt-d-expense-processor", D_TOOLS, VEC_D),
             _bundle("agt-b-payment-reminder", B_TOOLS, VEC_B)],
            now=NOW)
        self.assertEqual([s["bundle"]["agent_id"] for s in selected],
                         ["agt-b-payment-reminder", "agt-d-expense-processor"])
        self.assertEqual([s["tier"] for s in selected], ["same_job", "same_tooling"])
        self.assertEqual(selected[1]["shared_tools"],
                         ["gmail.search", "gmail.send", "sheets.append", "sheets.read"])


class JaccardTest(unittest.TestCase):
    def test_basics(self):
        self.assertEqual(peers.jaccard({"a", "b"}, {"a", "b"}), 1.0)
        self.assertAlmostEqual(peers.jaccard({"a", "b"}, {"b", "c"}), 1 / 3)
        self.assertEqual(peers.jaccard({"a"}, {"b"}), 0.0)

    def test_two_toolless_agents_have_nothing_in_common_not_everything(self):
        # An empty/empty union answering 1.0 would make every silent agent a peer of
        # every other one, and the top-K cut would fill with them.
        self.assertEqual(peers.jaccard(set(), set()), 0.0)
        self.assertEqual(peers.jaccard(None, None), 0.0)
        self.assertEqual(peers.jaccard({"a"}, set()), 0.0)


class CosineTest(unittest.TestCase):
    def test_basics(self):
        self.assertAlmostEqual(peers.cosine([1.0, 0.0], [3.0, 0.0]), 1.0)
        self.assertAlmostEqual(peers.cosine([1.0, 0.0], [0.0, 4.0]), 0.0)
        self.assertAlmostEqual(peers.cosine([1.0, 1.0], [-1.0, -1.0]), -1.0)

    def test_everything_incomparable_answers_zero(self):
        self.assertEqual(peers.cosine([1.0], [1.0, 2.0]), 0.0)      # mismatched lengths
        self.assertEqual(peers.cosine([0.0, 0.0], [1.0, 1.0]), 0.0)  # zero vector
        self.assertEqual(peers.cosine([1.0, 1.0], [0.0, 0.0]), 0.0)
        self.assertEqual(peers.cosine([], []), 0.0)
        self.assertEqual(peers.cosine(None, None), 0.0)
        self.assertEqual(peers.cosine(["nope"], [1.0]), 0.0)        # non-numeric


class ClassifyTest(unittest.TestCase):
    def test_a_missing_vector_reports_none_and_never_cosine_zero(self):
        # 0.0 is a measurement saying "different job"; None is the absence of one. The
        # figure travels into the peer payload, so an unembedded bundle must not arrive
        # there looking like it was measured and found unlike this agent.
        v = peers.classify(A_FP, _fp(B_TOOLS), None, VEC_B)
        self.assertTrue(v["included"])
        self.assertIsNone(v["cosine"])
        self.assertEqual(v["tier"], "same_tooling")
        self.assertIsNone(peers.classify(A_FP, _fp(B_TOOLS), VEC_A, None)["cosine"])
        self.assertIsNone(peers.classify(A_FP, _fp(B_TOOLS), [], [])["cosine"])

    def test_rank_orders_survivors_without_admitting_anyone(self):
        v = peers.classify(A_FP, _fp(B_TOOLS), None, None)
        self.assertAlmostEqual(v["rank"], 0.4 * (4 / 6))            # cosine term is 0
        self.assertTrue(v["included"])
        # A perfect cosine lifts rank but cannot flip inclusion for a stranger's tools.
        far = peers.classify(A_FP, _fp({"zendesk.reply": 4}), VEC_A, VEC_A)
        self.assertFalse(far["included"])

    def test_missing_or_malformed_fingerprints_do_not_raise(self):
        v = peers.classify(None, None, None, None)
        self.assertFalse(v["included"])
        self.assertEqual(v["overlap"], 0.0)
        self.assertEqual(peers.classify({"tools": "nope"}, A_FP, None, None)["overlap"],
                         0.0)


class SelectPeersTest(unittest.TestCase):
    def _select(self, bundles, agent_id="agt-a-invoice-chaser", now=NOW):
        return peers.select_peers(agent_id, A_FP, VEC_A, bundles, now=now)

    def test_own_unverified_and_stale_bundles_are_never_selectable(self):
        bundles = [_bundle("agt-a-invoice-chaser", B_TOOLS, VEC_B),
                   _bundle("agt-unverified", B_TOOLS, VEC_B, verified=False),
                   _bundle("agt-no-verified-key", B_TOOLS, VEC_B),
                   _bundle("agt-stale", B_TOOLS, VEC_B,
                           days_ago=config.PEER_BUNDLE_MAX_AGE_DAYS + 1),
                   _bundle("agt-keeper", B_TOOLS, VEC_B)]
        del bundles[2]["verified"]
        self.assertEqual([s["bundle"]["agent_id"] for s in self._select(bundles)],
                         ["agt-keeper"])

    def test_a_bundle_exactly_at_the_age_limit_is_still_fresh(self):
        selected = self._select([_bundle("agt-edge", B_TOOLS, VEC_B,
                                         days_ago=config.PEER_BUNDLE_MAX_AGE_DAYS)])
        self.assertEqual(len(selected), 1)

    def test_an_unparseable_created_at_does_not_empty_the_peer_list(self):
        # Dropping bundles because a storage backend changed its timestamp format would
        # look like a fact about the fleet ("this agent has no peers") rather than a bug.
        bundles = [_bundle("agt-odd", B_TOOLS, VEC_B, created_at="last Tuesday"),
                   _bundle("agt-none", B_TOOLS, VEC_B, created_at=None),
                   _bundle("agt-blank", B_TOOLS, VEC_B, created_at="   ")]
        self.assertEqual(len(self._select(bundles)), config.PEER_MAX)

    def test_ranked_descending_and_cut_at_peer_max(self):
        bundles = [_bundle("agt-4th", B_TOOLS, [0.30, 0.95, 0.00]),
                   _bundle("agt-1st", B_TOOLS, [1.00, 0.00, 0.00]),
                   _bundle("agt-5th", B_TOOLS, None),
                   _bundle("agt-3rd", B_TOOLS, [0.70, 0.70, 0.00]),
                   _bundle("agt-2nd", B_TOOLS, VEC_B)]
        selected = self._select(bundles)
        self.assertEqual(len(selected), config.PEER_MAX)
        self.assertEqual([s["bundle"]["agent_id"] for s in selected],
                         ["agt-1st", "agt-2nd", "agt-3rd"])
        ranks = [s["rank"] for s in selected]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_shared_tools_are_the_sorted_intersection(self):
        selected = self._select([_bundle("agt-b", B_TOOLS, VEC_B)])
        self.assertEqual(selected[0]["shared_tools"],
                         ["gmail.search", "gmail.send", "sheets.read",
                          "stripe.list_invoices"])

    def test_a_naive_now_does_not_take_selection_down(self):
        # Subtracting a naive datetime from an aware one raises TypeError, and a caller
        # passing datetime.utcnow() must not be what breaks peer selection.
        selected = self._select([_bundle("agt-b", B_TOOLS, VEC_B)],
                                now=NOW.replace(tzinfo=None))
        self.assertEqual(len(selected), 1)

    def test_empty_and_malformed_input_do_not_raise(self):
        self.assertEqual(peers.select_peers(None, None, None, None), [])
        self.assertEqual(self._select([]), [])
        self.assertEqual(self._select([None, "junk", 7, {}]), [])


class PeerPayloadsTest(unittest.TestCase):
    def _selected(self):
        return peers.select_peers(
            "agt-a-invoice-chaser", A_FP, VEC_A,
            [_bundle("agt-b-payment-reminder", B_TOOLS, VEC_B),
             _bundle("agt-d-expense-processor", D_TOOLS, VEC_D)],
            now=NOW)

    def test_labels_are_positional_and_in_the_ranked_order(self):
        payloads = peers.peer_payloads(self._selected())
        self.assertEqual([p["peer_ref"] for p in payloads],
                         ["peer agent 1", "peer agent 2"])
        self.assertEqual([p["tier"] for p in payloads], ["same_job", "same_tooling"])

    def test_no_identity_of_any_kind_reaches_the_payload(self):
        # The agent that receives this belongs to a DIFFERENT customer, and nothing it
        # is handed — nor anything a person later reads in the cycle's analysis or in a
        # log line quoting it — may be usable to work out whose material this was.
        bundle = _bundle("agt-7c1f-quokka", B_TOOLS, VEC_B,
                         owner="usr-9911-dana@quokka.example")
        bundle["agent_name"] = "Quokka Ltd invoice chaser"
        payloads = peers.peer_payloads(
            peers.select_peers("agt-a-invoice-chaser", A_FP, VEC_A, [bundle], now=NOW))
        blob = json.dumps(payloads, ensure_ascii=False, default=str)
        for leak in ("agt-7c1f-quokka", "usr-9911", "dana@quokka.example", "Quokka",
                     "agent_id", "owner_user_id", "agent_name", "embedding"):
            self.assertNotIn(leak, blob)
        self.assertIn("peer agent 1", blob)

    def test_payload_carries_the_material_and_nothing_else(self):
        payload = peers.peer_payloads(self._selected())[0]
        self.assertEqual(sorted(payload), ["memory", "peer_ref", "sessions",
                                           "shared_tools", "tier", "tool_overlap"])
        self.assertEqual(round(payload["tool_overlap"], 2), 0.67)
        self.assertEqual(payload["memory"],
                         {config.MEMORY_LESSONS_DOC: "Batch the searches, one per run."})
        self.assertEqual(payload["sessions"][0]["session_id"], "sess-1")

    def test_a_peer_that_does_not_fit_is_dropped_whole(self):
        # Half a peer's sessions still read as that agent's complete history, and the
        # analysis would generalise from the truncated half as if it were the story.
        first = _entry("sess-first", "the first peer's transcript, of ordinary length")
        huge = _entry("sess-huge", "x" * 5000)
        tiny = _entry("sess-tiny", "ok")
        budget = _cost(first) + _cost(tiny) + 50
        fitted = peers.peer_payloads([first, huge, tiny], budget_chars=budget)
        self.assertEqual(len(fitted), 1)
        # Whole, not clipped: the survivor's session text is byte for byte the original.
        self.assertEqual(fitted[0]["sessions"], first["bundle"]["sessions"])
        self.assertNotIn("sess-huge", json.dumps(fitted))
        # And the fill STOPS at the first peer that will not fit rather than skipping it
        # for the smaller one behind it: tiny had room to spare and is still absent. A
        # payload whose "peer agent 2" is really the third-ranked peer makes anything
        # the analysis later says about peer agent 2 impossible to trace back.
        self.assertNotIn("sess-tiny", json.dumps(fitted))
        self.assertEqual([p["peer_ref"] for p in fitted], ["peer agent 1"])

    def test_a_zero_or_unreadable_budget_admits_nobody(self):
        self.assertEqual(peers.peer_payloads(self._selected(), budget_chars=0), [])
        self.assertEqual(peers.peer_payloads(self._selected(), budget_chars=None), [])

    def test_empty_and_malformed_input_do_not_raise(self):
        self.assertEqual(peers.peer_payloads([]), [])
        self.assertEqual(peers.peer_payloads(None), [])
        self.assertEqual(peers.peer_payloads(["junk", None, 7]), [])
        payload = peers.peer_payloads([{"tier": "same_tooling"}])[0]
        self.assertEqual(payload["peer_ref"], "peer agent 1")
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(payload["memory"], {})
        self.assertEqual(payload["shared_tools"], [])


class FingerprintTest(unittest.TestCase):
    def test_counts_tool_calls_by_name_across_sessions(self):
        s1 = [{"type": "mcp_tool_use", "name": "gmail.search"},
              {"type": "assistant.tool_use", "name": "gmail.search"},
              {"type": "tool_use", "name": "sheets.read"}]
        s2 = [{"type": "mcp_tool_use", "name": "gmail.search"}]
        fp = peers.fingerprint([s1, s2])
        self.assertEqual(fp["tools"], {"gmail.search": 3, "sheets.read": 1})
        self.assertEqual(fp["sessions"], 2)

    def test_non_tool_events_are_ignored(self):
        events = [{"type": "user.message", "text": "go", "name": "gmail.search"},
                  {"type": "agent.message", "text": "done"},
                  {"type": "tool_result", "text": "ok", "name": "gmail.search"},
                  {"type": "session.status_idle", "text": ""}]
        self.assertEqual(peers.fingerprint([events])["tools"], {})

    def test_unnamed_calls_are_dropped_rather_than_bucketed(self):
        # _compact labels an unnamed call "tool"; a "tool" bucket here would intersect
        # with every other agent's unnamed calls and manufacture overlap between agents
        # that share no integration at all.
        fp = peers.fingerprint([[{"type": "mcp_tool_use"},
                                 {"type": "tool_use", "name": "  "}]])
        self.assertEqual(fp["tools"], {})

    def test_avg_tokens_is_the_mean_cost_per_session(self):
        s1 = [{"type": "agent.message", "text": "a",
               "usage": {"input_tokens": 100, "output_tokens": 20}}]
        s2 = [{"type": "agent.message", "text": "b",
               "usage": {"input_tokens": 40, "output_tokens": 0}}]
        fp = peers.fingerprint([s1, s2])
        self.assertEqual(fp["avg_tokens"], 80)

    def test_no_sessions_at_all(self):
        empty = {"tools": {}, "sessions": 0, "avg_tokens": 0}
        self.assertEqual(peers.fingerprint([]), empty)
        self.assertEqual(peers.fingerprint(None), empty)

    def test_an_empty_event_list_is_still_a_session(self):
        fp = peers.fingerprint([[]])
        self.assertEqual(fp["tools"], {})
        self.assertEqual(fp["sessions"], 1)

    def test_malformed_events_do_not_raise(self):
        fp = peers.fingerprint([None, 7, "nope",
                                [None, "x", {"type": "mcp_tool_use", "name": "gmail.search"}]])
        self.assertEqual(fp["tools"], {"gmail.search": 1})
        self.assertEqual(fp["sessions"], 1)


class ProfileTextTest(unittest.TestCase):
    def test_purpose_and_tools_most_used_first(self):
        text = peers.profile_text(
            {"name": "Invoice chaser", "description": "Chases unpaid invoices",
             "trigger": "Every weekday at 9"}, A_FP)
        self.assertIn("Invoice chaser", text)
        self.assertIn("Chases unpaid invoices", text)
        self.assertIn("gmail.search, sheets.read, stripe.list_invoices", text)

    def test_the_same_behaviour_always_embeds_to_the_same_point(self):
        # An unstable profile re-embeds slightly differently every cycle, and an agent's
        # cosines against unchanged peers drift for reasons that are not behavioural.
        a = {"tools": {"gmail.search": 3, "sheets.read": 3, "stripe.list_invoices": 3}}
        b = {"tools": {"stripe.list_invoices": 3, "gmail.search": 3, "sheets.read": 3}}
        self.assertEqual(peers.profile_text({"name": "x"}, a),
                         peers.profile_text({"name": "x"}, b))

    def test_a_toolless_agent_and_a_missing_agent_do_not_raise(self):
        self.assertIn("(none)", peers.profile_text(None, None))
        self.assertIn("(none)", peers.profile_text({"name": "x"}, {"tools": {}}))


if __name__ == "__main__":
    unittest.main()
