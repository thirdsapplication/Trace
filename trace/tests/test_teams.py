"""Teams (frame 30) — membership, invites, and the lesson tier they unlock.

The tier is the part with consequences: it decides whose lessons an agent can read.
match_lessons enforces it in SQL and registry.retrieve_lessons mirrors that by hand for
file mode, so both are exercised here — a sharing gate that disagrees with itself between
the two paths is the worst possible bug to leave in.
"""

import tempfile
import unittest

from trace.ui import app, registry, teams
from trace.ui import registry as reg


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


class OwnerRoleTest(unittest.TestCase):
    """The live database has a trigger that adds a team's creator as "owner". This
    module never writes that value, so nothing recognised it.

    Consequences, both hit by a real user on 2026-08-26: the first invite returned
    team_create_failed, because create() inserted the membership the trigger had
    already made and the duplicate-key error was swallowed into None — while the
    TEAM row stayed committed. And every action afterwards returned not_admin,
    because the admin check required exactly "admin".
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_owner_is_an_admin_role(self):
        self.assertIn("owner", teams.ADMIN_ROLES)
        self.assertIn("admin", teams.ADMIN_ROLES)
        self.assertNotIn("member", teams.ADMIN_ROLES)

    def test_a_trigger_created_owner_can_administer_their_team(self):
        """What the person who created the team hits on their very next click."""
        state = app.AppState(sessions_root=self.root)
        t = teams.create(self.root, "u1", "Thirds Foods")
        # Simulate the trigger's role, which is what the live rows actually carry.
        mem = reg._load(self.root, "team_members.json")
        for k in mem:
            mem[k]["role"] = "owner"
        reg._save(self.root, "team_members.json", mem)

        team, denied = state._team_admin({"user_id": "u1"})
        self.assertIsNone(denied, "the team's own creator was refused as not_admin")
        self.assertEqual(team["team_id"], t["team_id"])

    def test_a_plain_member_still_cannot_administer(self):
        """The fix must widen the door, not remove it."""
        state = app.AppState(sessions_root=self.root)
        t = teams.create(self.root, "u1", "A")
        teams.invite(self.root, t["team_id"], "b@x.com", "member", "u1")
        teams.accept_for(self.root, "b@x.com", "u2")
        _, denied = state._team_admin({"user_id": "u2"})
        self.assertEqual(denied, {"error": "not_admin"})


class TeamVisibilityTest(unittest.TestCase):
    """Seeing a teammate's agent must never become touching it.

    agents_for_user IS the ownership check: a dozen mutating endpoints are written as
    ``next(a for a in agents_for_user(...) if a.agent_id == x)`` and 404 when it
    misses. So visibility deliberately lives in a SEPARATE function, and the test
    that matters is the negative one — widening the guard would grant write access to
    every one of those endpoints at once, with no line that looks like a permission
    change.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        for owner, name in (("u1", "Mine"), ("u2", "Theirs")):
            registry.agent_upsert(self.root, {
                "agent_id": f"a-{owner}", "owner_user_id": owner, "name": name,
                "bundle_id": "B1", "owner_email": f"{owner}@x.com"})

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_teammates_agent_is_visible(self):
        seen = registry.agents_visible_to(self.root, "u1", ["u1", "u2"])
        self.assertEqual(sorted(a["name"] for a in seen), ["Mine", "Theirs"])

    def test_it_is_marked_as_theirs_and_not_editable(self):
        seen = registry.agents_visible_to(self.root, "u1", ["u1", "u2"])
        theirs = next(a for a in seen if a["name"] == "Theirs")
        self.assertEqual(theirs["can_edit"], False)
        self.assertEqual(theirs["shared_from"], "u2@x.com")

    def test_your_own_agents_are_not_marked_shared(self):
        seen = registry.agents_visible_to(self.root, "u1", ["u1", "u2"])
        mine = next(a for a in seen if a["name"] == "Mine")
        self.assertNotIn("shared_from", mine)
        self.assertNotIn("can_edit", mine)

    def test_THE_GUARD_DID_NOT_WIDEN(self):
        """The one that matters. If agents_for_user ever returns a teammate's agent,
        every mutating endpoint silently accepts it."""
        owned = registry.agents_for_user(self.root, "u1")
        self.assertEqual([a["name"] for a in owned], ["Mine"],
                         "agents_for_user leaked a teammate's agent — this is write access")

    def test_no_team_changes_nothing(self):
        self.assertEqual(
            [a["name"] for a in registry.agents_visible_to(self.root, "u1", [])],
            ["Mine"])

    def test_a_shared_agent_is_never_listed_twice(self):
        seen = registry.agents_visible_to(self.root, "u1", ["u1", "u1", "u2", "u2"])
        self.assertEqual(len(seen), 2)


class UseAsTemplateTest(unittest.TestCase):
    """Templating must not become a back door to someone else's agent or recording.

    An agent is welded to its creator's Pipedream vault, so it cannot change hands;
    and their session holds a recording of THEIR screen, so it must not be copied.
    What travels is the workflow description, into a session owned by the caller.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        registry.agent_upsert(self.root, {
            "agent_id": "a-theirs", "owner_user_id": "u2", "name": "Order entry",
            "bundle_id": "B1", "owner_email": "u2@x.com",
            "description": "Copy each order email into the ERP sheet",
            "trigger": "a new order email arrives"})

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_teammates_agent_is_templatable_and_a_strangers_is_not(self):
        mates = registry.agents_visible_to(self.root, "u1", ["u1", "u2"])
        self.assertTrue(any(a["agent_id"] == "a-theirs" for a in mates))
        alone = registry.agents_visible_to(self.root, "u1", [])
        self.assertFalse(any(a["agent_id"] == "a-theirs" for a in alone),
                         "an agent outside your team was templatable")

    def test_templating_never_transfers_the_agent(self):
        """The recipient must end up building their OWN, not holding theirs."""
        before = [a["agent_id"] for a in registry.agents_for_user(self.root, "u1")]
        registry.agents_visible_to(self.root, "u1", ["u1", "u2"])
        after = [a["agent_id"] for a in registry.agents_for_user(self.root, "u1")]
        self.assertEqual(before, after)
        still_theirs = [a["agent_id"] for a in registry.agents_for_user(self.root, "u2")]
        self.assertIn("a-theirs", still_theirs)


if __name__ == "__main__":
    unittest.main()
