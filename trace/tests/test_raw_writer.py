import json
import os
import tempfile
import unittest

from trace.capture.raw_writer import RawWriter, now_iso


class RawWriterTest(unittest.TestCase):
    def test_writes_normalized_record(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            w = RawWriter(p, reset=True)
            w.write("window", {"app": "Mail", "title": "Inbox"})
            with open(p) as f:
                rec = json.loads(f.readline())
            self.assertEqual(rec["source"], "window")
            self.assertEqual(rec["payload"]["title"], "Inbox")
            self.assertIn("ts", rec)

    def test_redacts_before_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            w = RawWriter(p, reset=True)
            w.write("clipboard", {"op": "copy", "content": "ssn 123-45-6789"})
            with open(p) as f:
                rec = json.loads(f.readline())
            self.assertNotIn("123-45-6789", rec["payload"]["content"])

    def test_pause_suppresses_writes(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            w = RawWriter(p, reset=True)
            w.pause()
            self.assertIsNone(w.write("window", {"app": "A", "title": "B"}))
            w.resume()
            self.assertIsNotNone(w.write("window", {"app": "A", "title": "B"}))
            with open(p) as f:
                self.assertEqual(len(f.readlines()), 1)

    def test_unknown_source_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            w = RawWriter(os.path.join(d, "raw.jsonl"), reset=True)
            with self.assertRaises(ValueError):
                w.write("screenshot", {})

    def test_now_iso_has_offset(self):
        # ISO-8601 with tz offset, ms precision
        ts = now_iso()
        self.assertRegex(ts, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}")


if __name__ == "__main__":
    unittest.main()
