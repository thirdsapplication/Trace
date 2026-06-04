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
