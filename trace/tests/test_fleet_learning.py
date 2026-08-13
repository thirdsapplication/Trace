"""Gap #3 B5 (Phase B) storage: consent resolution (per-user default + per-agent override),
lesson promotions (provisional→confirmed→retired), the distinct-user confirm count that
drives fleet-default graduation, provenance, and agent-kind embeddings. File-mode, offline."""

import json
import os
import tempfile
import unittest

from trace.ui import registry


class FleetStorageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_consent_defaults_global_and_set(self):
        self.assertEqual(registry.user_consent_get(self.root, "u1"),
                         {"contribute_scope": "global", "consume_scope": "global"})  # opt-out posture
        registry.user_consent_set(self.root, "u1", consume_scope="owner")
        self.assertEqual(registry.user_consent_get(self.root, "u1")["consume_scope"], "owner")
        registry.user_consent_set(self.root, "u1", contribute_scope="bogus")          # invalid ignored
        self.assertEqual(registry.user_consent_get(self.root, "u1")["contribute_scope"], "global")

    def test_agent_override_beats_user_default(self):
        registry.user_consent_set(self.root, "u1", consume_scope="team")
        s = registry.agent_lesson_scopes(self.root, {"owner_user_id": "u1",
                                                     "lesson_consume_scope": "owner"})
        self.assertEqual(s["consume_scope"], "owner")        # agent override wins
        self.assertEqual(s["contribute_scope"], "global")    # inherits user default

    def test_agent_row_carries_its_consent_override_back(self):
        """agent_set_consent writes lesson_contribute_scope / lesson_consume_scope to the
        agents table (008 added them, NULL = inherit) and _agent_to_caller never read them
        back. So in Supabase mode agent_lesson_scopes always fell through to the account
        default: an override could be set, stored and displayed, and never applied.

        The test above cannot catch it — it hands agent_lesson_scopes a dict with the
        override already in it, so the row mapping is never exercised. Exactly the same
        omission as `color`, which had the same shape and the same blind spot.
        """
        row = {"id": "agent_1", "owner_id": "u1", "name": "Portal audit",
               "lesson_contribute_scope": "owner", "lesson_consume_scope": None}
        agent = registry._agent_to_caller(row)
        self.assertEqual(agent["lesson_contribute_scope"], "owner")
        self.assertIsNone(agent["lesson_consume_scope"])       # NULL stays inherit

        registry.user_consent_set(self.root, "u1", contribute_scope="global",
                                  consume_scope="global")
        s = registry.agent_lesson_scopes(self.root, dict(agent, owner_user_id="u1"))
        self.assertEqual(s["contribute_scope"], "owner")       # the override survives
        self.assertEqual(s["consume_scope"], "global")         # the unset one inherits

    def test_promotion_lifecycle_and_distinct_user_count(self):
        registry.lesson_promotion_upsert(self.root, "a1", "u1", "d1")
        self.assertEqual(registry.lesson_promotion_get(self.root, "a1", "d1")["status"], "provisional")
        registry.lesson_promotion_upsert(self.root, "a1", "u1", "d1", status="confirmed")  # idempotent
        self.assertEqual(registry.lesson_promotion_get(self.root, "a1", "d1")["status"], "provisional")
        registry.lesson_promotion_set_status(self.root, "a1", "d1", "confirmed")
        self.assertEqual(registry.lesson_promotion_get(self.root, "a1", "d1")["status"], "confirmed")
        # two agents of the SAME user count as one confirming user
        registry.lesson_promotion_upsert(self.root, "a2", "u1", "d1")
        registry.lesson_promotion_set_status(self.root, "a2", "d1", "confirmed")
        self.assertEqual(registry.lesson_confirmed_user_count(self.root, "d1"), 1)
        registry.lesson_promotion_upsert(self.root, "a3", "u2", "d1")
        registry.lesson_promotion_set_status(self.root, "a3", "d1", "confirmed")
        self.assertEqual(registry.lesson_confirmed_user_count(self.root, "d1"), 2)

    def test_retire(self):
        registry.lesson_promotion_upsert(self.root, "a1", "u1", "d1")
        registry.lesson_promotion_set_status(self.root, "a1", "d1", "retired")
        self.assertEqual(registry.lesson_promotion_get(self.root, "a1", "d1")["status"], "retired")

    def test_application_provenance_persisted(self):
        registry.lesson_application_add(self.root, "a1", "u1", "d1", "sess-1")
        apps = json.load(open(os.path.join(registry._dir(self.root), "applications.json")))
        self.assertEqual(len(apps), 1)
        self.assertEqual(list(apps.values())[0]["decision_id"], "d1")

    def test_agent_embedding_round_trip(self):
        registry.agent_embedding_upsert(self.root, "a1", [0.1] * 1024)
        self.assertEqual(len(registry.agent_embedding_get(self.root, "a1")), 1024)


class RetrieveLessonsTest(unittest.TestCase):
    """B5b: scoped, double-gated retrieval (own vs cross, agent-kind gate, higher bar)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self, owner, agent_id, lesson, sit_emb, kind_emb=None, global_verified=True):
        registry.decision_add(self.root, {"agent_id": agent_id, "owner_user_id": owner,
            "run_session_id": "s" + lesson, "question": "q" + lesson, "context": {}})
        d = [x for x in registry.decisions_for_user(self.root, owner)
             if x["question"] == "q" + lesson][0]
        registry.decision_set_generalization(self.root, d["id"], True, lesson)
        registry.decision_set_global_verified(self.root, d["id"], global_verified)
        registry.decision_embedding_add(self.root, d["id"], sit_emb)
        if kind_emb is not None:
            registry.agent_embedding_upsert(self.root, agent_id, kind_emb)
        return d

    def test_cross_lesson_requires_global_verified(self):
        self._seed("u2", "a2", "Unverified cross rule.", [1.0, 0.0], kind_emb=[1.0, 0.0],
                   global_verified=False)
        agent = {"owner_user_id": "u1", "agent_id": "aq"}
        self.assertEqual(registry.retrieve_lessons(self.root, [1.0, 0.0], agent,
                         agent_kind_embedding=[1.0, 0.0], consume_scope="global"), [])

    def test_own_lesson_returned_at_low_bar(self):
        self._seed("u1", "a1", "Own rule.", [1.0, 0.0])
        hits = registry.retrieve_lessons(self.root, [1.0, 0.1],
                                         {"owner_user_id": "u1", "agent_id": "aq"},
                                         consume_scope="owner")
        self.assertEqual([h["tier"] for h in hits], ["own"])
        self.assertEqual(hits[0]["lesson"], "Own rule.")

    def test_cross_lesson_requires_global_scope(self):
        self._seed("u2", "a2", "Cross rule.", [1.0, 0.0], kind_emb=[1.0, 0.0])
        agent = {"owner_user_id": "u1", "agent_id": "aq"}
        self.assertEqual(registry.retrieve_lessons(self.root, [1.0, 0.0], agent,
                         agent_kind_embedding=[1.0, 0.0], consume_scope="owner"), [])   # owner-scoped: none
        hits = registry.retrieve_lessons(self.root, [1.0, 0.0], agent,
                                         agent_kind_embedding=[1.0, 0.0], consume_scope="global")
        self.assertEqual([h["tier"] for h in hits], ["cross"])

    def test_cross_lesson_is_agent_kind_gated(self):
        self._seed("u2", "a2", "Cross rule.", [1.0, 0.0], kind_emb=[1.0, 0.0])
        agent = {"owner_user_id": "u1", "agent_id": "aq"}
        # dissimilar agent kind -> filtered
        self.assertEqual(registry.retrieve_lessons(self.root, [1.0, 0.0], agent,
                         agent_kind_embedding=[0.0, 1.0], consume_scope="global"), [])
        # missing querying-agent kind embedding -> filtered (never leak unverified)
        self.assertEqual(registry.retrieve_lessons(self.root, [1.0, 0.0], agent,
                         agent_kind_embedding=None, consume_scope="global"), [])

    def test_cross_lesson_has_higher_situation_bar(self):
        self._seed("u2", "a2", "Cross rule.", [1.0, 0.0], kind_emb=[1.0, 0.0])
        agent = {"owner_user_id": "u1", "agent_id": "aq"}
        # sim 0.6 clears the 0.5 own bar but not the 0.65 cross bar -> filtered
        self.assertEqual(registry.retrieve_lessons(self.root, [0.6, 0.8], agent,
                         agent_kind_embedding=[1.0, 0.0], consume_scope="global"), [])
        self.assertEqual(len(registry.retrieve_lessons(self.root, [1.0, 0.0], agent,
                         agent_kind_embedding=[1.0, 0.0], consume_scope="global")), 1)


if __name__ == "__main__":
    unittest.main()
