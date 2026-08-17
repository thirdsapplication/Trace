"""Teams (frame 30) — membership, invites, and the lesson tier they unlock.

The tier is the part with consequences: it decides whose lessons an agent can read.
match_lessons enforces it in SQL and registry.retrieve_lessons mirrors that by hand for
file mode, so both are exercised here — a sharing gate that disagrees with itself between
the two paths is the worst possible bug to leave in.
"""

import tempfile
import unittest

from trace.ui import registry, teams


class TeamMembershipTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_team_is_a_normal_state(self):
        self.assertIsNone(teams.team_for_user(self.root, "u1"))
        self.assertEqual(teams.owner_ids(self.root, "u1"), [])

    def test_invite_then_signup_puts_them_in_the_team(self):
        """The whole point of an invite. Without accept_for at signup it is a row that
        gets created, listed, resent and cancelled and never puts anybody in a team."""
        t = teams.create(self.root, "u1", "Thirds Foods")
        self.assertEqual(t["role"], "admin")
        teams.invite(self.root, t["team_id"], "New@Thirds.co", "member", "u1")
        self.assertEqual([i["email"] for i in teams.pending_invites(self.root, t["team_id"])],
                         ["new@thirds.co"])                # normalised
        joined = teams.accept_for(self.root, "new@thirds.co", "u2")
        self.assertEqual(joined["team_id"], t["team_id"])
        self.assertEqual(joined["role"], "member")
        self.assertEqual(teams.pending_invites(self.root, t["team_id"]), [])  # consumed
        self.assertEqual(sorted(teams.owner_ids(self.root, "u1")), ["u1", "u2"])

    def test_accept_is_idempotent_and_never_moves_someone(self):
        a = teams.create(self.root, "u1", "A")
        teams.create(self.root, "u9", "B")
        teams.invite(self.root, a["team_id"], "u9@x.com", "member", "u1")
        self.assertIsNone(teams.accept_for(self.root, "u9@x.com", "u9"))
        self.assertEqual(teams.team_for_user(self.root, "u9")["name"], "B")

    def test_create_is_idempotent(self):
        first = teams.create(self.root, "u1", "A")
        again = teams.create(self.root, "u1", "Something else")
        self.assertEqual(first["team_id"], again["team_id"])
        self.assertEqual(again["name"], "A")               # not renamed behind your back

    def test_reinvite_refreshes_rather_than_duplicating(self):
        """The UI offers Resend, so a second invite to one address has to mean refresh."""
        t = teams.create(self.root, "u1", "A")
        teams.invite(self.root, t["team_id"], "x@y.com", "member", "u1")
        teams.invite(self.root, t["team_id"], "x@y.com", "admin", "u1")
        pend = teams.pending_invites(self.root, t["team_id"])
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["role"], "admin")

    def test_roles_and_removal(self):
        t = teams.create(self.root, "u1", "A")
        teams.invite(self.root, t["team_id"], "b@x.com", "member", "u1")
        teams.accept_for(self.root, "b@x.com", "u2")
        teams.set_role(self.root, t["team_id"], "u2", "admin")
        self.assertEqual(teams.team_for_user(self.root, "u2")["role"], "admin")
        teams.set_role(self.root, t["team_id"], "u2", "nonsense")     # ignored
        self.assertEqual(teams.team_for_user(self.root, "u2")["role"], "admin")
        teams.remove_member(self.root, t["team_id"], "u2")
        self.assertIsNone(teams.team_for_user(self.root, "u2"))
        self.assertEqual(teams.owner_ids(self.root, "u1"), ["u1"])


class TeamLessonTierTest(unittest.TestCase):
    """The tier that migration 018 added and nothing was passing arguments to."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        # A teammate's reusable lesson. NOT global_verified on purpose: sharing with a
        # colleague must not require having published to strangers.
        registry.decision_add(self.root, {
            "agent_id": "a-mate", "owner_user_id": "u2", "run_session_id": "s1",
            "question": "Ship date missing?", "context": {}})
        did = registry.decisions_for_user(self.root, "u2")[0]["id"]
        registry.decision_set_generalization(self.root, did, True,
                                             "Assume standard 5-day shipping")
        registry.decision_embedding_add(self.root, did, [1.0, 0.0])
        self.agent = {"agent_id": "a-mine", "owner_user_id": "u1"}

    def tearDown(self):
        self._tmp.cleanup()

    def _hits(self, scope, mates):
        return registry.retrieve_lessons(self.root, [1.0, 0.0], self.agent,
                                         consume_scope=scope, team_owners=mates)

    def test_no_team_means_no_teammate_lessons(self):
        """Byte-for-byte the old behaviour, which is what makes adopting this safe."""
        self.assertEqual(self._hits("team", []), [])

    def test_a_teammates_lesson_is_reachable_at_team_scope(self):
        registry.user_consent_set(self.root, "u2", contribute_scope="team")
        hits = self._hits("team", ["u1", "u2"])
        self.assertEqual([h["tier"] for h in hits], ["team"])
        self.assertEqual(hits[0]["source_owner_id"], "u2")

    def test_a_teammate_who_shares_nothing_is_not_read(self):
        """Their consent decides, not their membership."""
        registry.user_consent_set(self.root, "u2", contribute_scope="owner")
        self.assertEqual(self._hits("team", ["u1", "u2"]), [])

    def test_global_scope_still_includes_teammates(self):
        """'global' is the WIDER setting — it cannot exclude the narrower one."""
        registry.user_consent_set(self.root, "u2", contribute_scope="team")
        self.assertEqual([h["tier"] for h in self._hits("global", ["u1", "u2"])], ["team"])

    def test_owner_scope_ignores_the_team_entirely(self):
        registry.user_consent_set(self.root, "u2", contribute_scope="global")
        self.assertEqual(self._hits("owner", ["u1", "u2"]), [])


if __name__ == "__main__":
    unittest.main()
