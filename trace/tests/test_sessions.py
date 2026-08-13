import json
import os
import tempfile
import unittest

from trace.ui import sessions


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_and_meta(self):
        sid = sessions.create_session(self.root, label="run A")
        meta = sessions.read_meta(self.root, sid)
        self.assertEqual(meta["label"], "run A")
        self.assertIsNone(meta["reconstructed_at"])
        self.assertTrue(os.path.isdir(sessions.session_dir(self.root, sid)))

    def test_artifacts_reflect_files(self):
        sid = sessions.create_session(self.root)
        art = sessions.artifacts(self.root, sid)
        self.assertFalse(art["raw"])
        # write a raw stream
        with open(sessions.path(self.root, sid, sessions.RAW), "w") as f:
            f.write(json.dumps({"ts": "t", "source": "window", "payload": {}}) + "\n")
            f.write(json.dumps({"ts": "t", "source": "keystroke", "payload": {}}) + "\n")
        art = sessions.artifacts(self.root, sid)
        self.assertTrue(art["raw"])
        self.assertEqual(art["raw_event_count"], 2)
        self.assertFalse(art["event_log"])

    def test_list_sorted_desc(self):
        a = sessions.create_session(self.root)
        # force a second, later id
        import time; time.sleep(1.05)
        b = sessions.create_session(self.root)
        ids = [s["id"] for s in sessions.list_sessions(self.root)]
        self.assertEqual(ids[:2], sorted([a, b], reverse=True))

    def test_supabase_listing_keeps_wizard_progress_from_meta(self):
        """Reported 2026-08-12: an agent deployed successfully and the wizard then went
        back to Review.

        deriveStep decides where you are from reviewed_at / interview_complete_at /
        build_at / deploy_at. All four are written by update_meta — to meta.json — and
        NONE is synced to Supabase. In Supabase mode list_sessions builds each meta from
        the database row alone, so every one came back absent and the session reported no
        progress whatever it had actually done.

        This exercises the row→meta merge directly; the Supabase branch itself needs a
        live PostgREST, which the offline suite has no way to stand up.
        """
        sid = sessions.create_session(self.root)
        sessions.update_meta(self.root, sid, reviewed_at="2026-08-12T00:00:00Z",
                             interview_complete_at="2026-08-12T00:05:00Z",
                             build_at="2026-08-12T00:10:00Z",
                             deploy_at="2026-08-12T00:15:00Z")
        row = {"local_sid": sid, "id": "11111111-1111-1111-1111-111111111111",
               "owner_id": "u1", "created_at": "2026-08-12T00:00:00Z"}
        meta = sessions._sb_row_to_meta(row)
        # Straight from the row, the progress simply isn't there — that IS the bug.
        for k in sessions._PROGRESS_FIELDS:
            self.assertIsNone(meta.get(k))
        # The merge list_sessions performs restores it.
        local = sessions.read_meta(self.root, sid)
        for k in sessions._PROGRESS_FIELDS:
            if local.get(k) is not None:
                meta[k] = local[k]
        self.assertEqual(meta["reviewed_at"], "2026-08-12T00:00:00Z")
        self.assertEqual(meta["build_at"], "2026-08-12T00:10:00Z")
        self.assertEqual(meta["deploy_at"], "2026-08-12T00:15:00Z")

    def test_row_mapping_has_no_phantom_progress_keys(self):
        """It used to return built_at and deployed_at — columns nothing writes, under
        names deriveStep never reads. A value there was worse than none: it looked like
        the progress was handled."""
        meta = sessions._sb_row_to_meta({"local_sid": "s1", "id": "x", "owner_id": "u"})
        self.assertNotIn("built_at", meta)
        self.assertNotIn("deployed_at", meta)

    def test_read_raw_events(self):
        sid = sessions.create_session(self.root)
        with open(sessions.path(self.root, sid, sessions.RAW), "w") as f:
            f.write(json.dumps({"ts": "t", "source": "window", "payload": {"app": "A"}}) + "\n")
        rows = sessions.read_raw_events(self.root, sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["app"], "A")

    def test_read_missing_artifact_is_none(self):
        sid = sessions.create_session(self.root)
        self.assertIsNone(sessions.read_json_artifact(self.root, sid, sessions.EVENT_LOG))


if __name__ == "__main__":
    unittest.main()
