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
        # Shift is not a shortcut modifier — it produces ordinary uppercase, so a
        # character typed while it is held is still typed text.
        self.feed(["a", "Key.shift", "b"])
        self.c.flush(ts_end="t")
        self.assertEqual(self.w.events[0]["payload"]["text"], "ab")

    def test_shortcut_chars_not_recorded_as_text(self):
        """Cmd+C / Cmd+V must not land in the buffer as 'c' and 'v'. Copy-paste-heavy
        work used to record as `cvcvcvcv`, which reads as garbled input downstream and
        has made discovery refuse to automate an otherwise clean workflow."""
        self.feed(["S", "h", "i", "p"])
        self.c.on_key("Key.cmd", ts="t")
        self.feed(["c", "v", "v"])
        self.c.on_key_release("Key.cmd")
        self.feed(["p", "e", "d"])
        self.c.flush(ts_end="t")
        self.assertEqual(self.w.events[0]["payload"]["text"], "Shipped")

    def test_modifier_release_restores_typing(self):
        """A held modifier must be cleared on release, or every subsequent keystroke
        would be swallowed as part of a shortcut."""
        self.c.on_key("Key.cmd", ts="t")
        self.feed(["c"])
        self.c.on_key_release("Key.cmd")
        self.feed(["h", "i"])
        self.c.flush(ts_end="t")
        self.assertEqual(self.w.events[0]["payload"]["text"], "hi")

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
