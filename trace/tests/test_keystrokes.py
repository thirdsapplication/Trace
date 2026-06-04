import unittest

from trace.capture.keystrokes import KeystrokeCoalescer


class FakeWriter:
    def __init__(self):
        self.events = []

    def write(self, source, payload, ts=None):
        rec = {"ts": ts, "source": source, "payload": payload}
        self.events.append(rec)
        return rec


class KeystrokeCoalescingTest(unittest.TestCase):
    def setUp(self):
        self.w = FakeWriter()
        self.c = KeystrokeCoalescer(self.w)

    def feed(self, keys):
        for i, k in enumerate(keys):
            self.c.on_key(k, ts=f"2026-06-04T10:00:0{i}.000")

    def test_backspace_applied(self):
        self.feed(["h", "e", "l", "x", "Key.backspace", "l", "o"])
        self.c.set_window("Doc")
        self.c.flush(ts_end="2026-06-04T10:00:09.000")
        self.assertEqual(self.w.events[0]["payload"]["text"], "hello")

    def test_special_keys_marked(self):
        self.feed(["h", "i", "Key.tab", "Key.space", "Key.enter"])
        self.c.flush(ts_end="t")
        text = self.w.events[0]["payload"]["text"]
        self.assertIn("[TAB]", text)
        self.assertIn("[ENTER]", text)

    def test_modifier_keys_dropped(self):
        self.feed(["a", "Key.shift", "Key.ctrl", "b"])
        self.c.flush(ts_end="t")
        self.assertEqual(self.w.events[0]["payload"]["text"], "ab")

    def test_empty_segment_not_emitted(self):
        self.c.flush(ts_end="t")
        self.assertEqual(self.w.events, [])

    def test_window_attribution(self):
        self.c.set_window("Sheet")
        self.feed(["x"])
        self.c.flush(ts_end="t")
        self.assertEqual(self.w.events[0]["payload"]["window"], "Sheet")

    def test_segment_start_ts_is_first_key(self):
        self.feed(["a", "b"])
        self.c.flush(ts_end="end")
        self.assertEqual(self.w.events[0]["ts"], "2026-06-04T10:00:00.000")


if __name__ == "__main__":
    unittest.main()
