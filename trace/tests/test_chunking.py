import json
import os
import tempfile
import unittest

from trace.reconstruct import chunking


def make_raw(n):
    return [{"id": f"r{i:04d}", "ts": f"2026-06-04T10:00:{i:02d}.000",
             "source": "window", "payload": {"app": "A", "title": f"w{i}"}}
            for i in range(1, n + 1)]


class ChunkingTest(unittest.TestCase):
    def test_load_orders_and_ids(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            with open(p, "w") as f:
                # write out of order
                f.write(json.dumps({"ts": "2026-06-04T10:00:05.000", "source": "window",
                                    "payload": {"app": "A", "title": "late"}}) + "\n")
                f.write(json.dumps({"ts": "2026-06-04T10:00:01.000", "source": "window",
                                    "payload": {"app": "A", "title": "early"}}) + "\n")
            raw = chunking.load_raw(p)
            self.assertEqual(raw[0]["id"], "r0001")
            self.assertEqual(raw[0]["payload"]["title"], "early")
            self.assertEqual(raw[1]["payload"]["title"], "late")

    def test_single_chunk_when_small(self):
        self.assertEqual(len(chunking.chunk(make_raw(10), chunk_size=120)), 1)

    def test_overlap_repeats_boundary_events(self):
        chunks = chunking.chunk(make_raw(6), chunk_size=4, overlap=2)
        self.assertEqual(len(chunks), 2)
        # last 2 of chunk 0 == first 2 of chunk 1
        self.assertEqual([e["id"] for e in chunks[0][-2:]],
                         [e["id"] for e in chunks[1][:2]])

    def test_chunks_cover_all_events(self):
        chunks = chunking.chunk(make_raw(20), chunk_size=7, overlap=2)
        covered = set()
        for ch in chunks:
            covered |= {e["id"] for e in ch}
        self.assertEqual(len(covered), 20)

    def test_render_keystroke(self):
        ev = {"id": "r0001", "ts": "t", "source": "keystroke",
              "payload": {"text": "hello", "window": "Doc", "ts_end": "t2"}}
        line = chunking.render_event(ev)
        self.assertIn("r0001", line)
        self.assertIn("hello", line)
        self.assertIn("TYPED", line)

    def test_render_clipboard(self):
        ev = {"id": "r0002", "ts": "t", "source": "clipboard",
              "payload": {"op": "copy", "content": "SKU-9"}}
        self.assertIn("CLIPBOARD", chunking.render_event(ev))
        self.assertIn("SKU-9", chunking.render_event(ev))

    def test_email_body_text_reaches_the_model_whole(self):
        body = ("I am writing to inform you of an order arriving 09/29/2027, "
                "for product \u201cNapkins\u201d in quantity 10.")
        row = ("Unread Philip Oravitan Order Napkins 11:46 PM Hello, " + body +
               " Please confirm receipt of this email. Thanks.")
        click = {"id": "r0001", "ts": "t", "source": "click",
                 "payload": {"button": "left", "x": 1, "y": 2,
                             "target": {"role": "text", "label": body}}}
        focus = {"id": "r0002", "ts": "t", "source": "target",
                 "payload": {"role": "row", "label": row}}
        self.assertIn("in quantity 10.", chunking.render_event(click))
        self.assertIn("Please confirm receipt", chunking.render_event(focus))

    def test_summarize_empty(self):
        self.assertEqual(chunking.summarize([]), "(none yet)")


if __name__ == "__main__":
    unittest.main()
