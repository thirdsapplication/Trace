"""Outbound notifications: the delivery gate, the preference gate, and the promise
that neither can break a run.

The property that matters most here is NEGATIVE: with no RESEND_API_KEY the module
does nothing, says so, and never touches the network. That is the state the app ships
in until a key and a verified sending domain exist, so it is the state most worth
pinning down.
"""

import os
import tempfile
import unittest
from unittest import mock

from trace.ui import notify, registry


class _Env:
    """Set/clear the two env vars without leaking into other tests."""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class DeliveryGateTest(unittest.TestCase):
    def test_inert_without_a_key(self):
        with _Env(RESEND_API_KEY=None, EMAIL_FROM="palza <n@x.com>"):
            self.assertFalse(notify.is_configured())
            with mock.patch("urllib.request.urlopen") as sent:
                self.assertFalse(notify.send("a@b.com", "s", "t"))
                sent.assert_not_called()          # no key => no network, at all

    def test_inert_without_a_from_address(self):
        """A key alone is not enough: Resend refuses an unverified sender, so sending
        without EMAIL_FROM would be a guaranteed 4xx on every notification."""
        with _Env(RESEND_API_KEY="re_test", EMAIL_FROM=None):
            self.assertFalse(notify.is_configured())
            with mock.patch("urllib.request.urlopen") as sent:
                self.assertFalse(notify.send("a@b.com", "s", "t"))
                sent.assert_not_called()

    def test_configured_send_posts_to_resend(self):
        with _Env(RESEND_API_KEY="re_test", EMAIL_FROM="palza <n@x.com>"):
            self.assertTrue(notify.is_configured())
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.status = 200
                self.assertTrue(notify.send("a@b.com", "Subject", "Body"))
                req = urlopen.call_args[0][0]
                self.assertEqual(req.full_url, "https://api.resend.com/emails")
                self.assertEqual(req.get_header("Authorization"), "Bearer re_test")

    def test_a_refusal_is_reported_not_raised(self):
        """A bad key or unverified domain is the likeliest failure in production, and it
        happens inside a run path. It must come back as False, never as an exception."""
        with _Env(RESEND_API_KEY="re_bad", EMAIL_FROM="palza <n@x.com>"):
            with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
                self.assertFalse(notify.send("a@b.com", "s", "t"))


class PreferenceGateTest(unittest.TestCase):
    def test_blocked_ping_respects_the_toggle(self):
        with _Env(RESEND_API_KEY="re_test", EMAIL_FROM="palza <n@x.com>"):
            with mock.patch.object(notify, "send", return_value=True) as send:
                notify.agent_needs_you({"notify_agent_blocked": False},
                                       "a@b.com", "Retail recap", "hi")
                send.assert_not_called()
                notify.agent_needs_you({"notify_agent_blocked": True},
                                       "a@b.com", "Retail recap", "hi")
                self.assertIn("Retail recap", send.call_args[0][1])

    def test_invite_is_not_preference_gated(self):
        """An invitation is the mechanism by which someone joins, not a notification —
        and the recipient may have no account, so there is no preference to consult."""
        with _Env(RESEND_API_KEY="re_test", EMAIL_FROM="palza <n@x.com>"):
            with mock.patch.object(notify, "send", return_value=True) as send:
                notify.team_invite("new@x.com", "Thirds Foods", "sam@x.com", "https://x/i")
                send.assert_called_once()
                self.assertIn("https://x/i", send.call_args[0][2])


class NotifyPrefsStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_match_the_frame(self):
        p = registry.user_notify_prefs(self.root, "u1")
        self.assertEqual(p, {"notify_morning_brief": True, "notify_agent_blocked": True,
                             "notify_weekly_digest": False})

    def test_set_then_read_round_trips_and_ignores_junk(self):
        registry.user_notify_set(self.root, "u1", notify_weekly_digest=True,
                                 notify_agent_blocked=False,
                                 nonsense="x", notify_morning_brief="yes")
        p = registry.user_notify_prefs(self.root, "u1")
        self.assertTrue(p["notify_weekly_digest"])
        self.assertFalse(p["notify_agent_blocked"])
        self.assertTrue(p["notify_morning_brief"])          # non-bool ignored, default kept

    def test_users_are_isolated(self):
        registry.user_notify_set(self.root, "u1", notify_agent_blocked=False)
        self.assertTrue(registry.user_notify_prefs(self.root, "u2")["notify_agent_blocked"])


if __name__ == "__main__":
    unittest.main()
