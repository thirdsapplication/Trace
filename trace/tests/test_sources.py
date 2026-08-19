"""Item 3 — capture observability + fail-loud signal for missing browser capture.

The by-source summary is generic over whatever sources appear (a new source needs
no code change), and the warning is defined in terms of source-origin metadata and
a configurable threshold — not tuned to any run's specific counts.
"""

import unittest

from trace.capture import sources


def rows_with(**counts):
    out = []
    for src, n in counts.items():
        for _ in range(n):
            out.append({"ts": "t", "source": src, "payload": {}})
    return out


class SummaryTest(unittest.TestCase):
    def test_counts_by_source_generic_including_unknown(self):
        rows = rows_with(window=3, keystroke=5, click=2)
        # an unfamiliar source type still gets counted, no code change needed
        rows.append({"ts": "t", "source": "future_sensor", "payload": {}})
        summary = sources.summarize_sources(rows)
        self.assertEqual(summary["window"], 3)
        self.assertEqual(summary["keystroke"], 5)
        self.assertEqual(summary["click"], 2)
        self.assertEqual(summary["future_sensor"], 1)

    def test_missing_source_field_counts_as_unknown(self):
        summary = sources.summarize_sources([{"ts": "t", "payload": {}}])
        self.assertEqual(summary["unknown"], 1)


class WarningTest(unittest.TestCase):
    def test_local_only_session_warns(self):
        health = sources.capture_health(sources.summarize_sources(
            rows_with(window=10, keystroke=20)))
        self.assertTrue(health["warning"])
        self.assertIsNotNone(health["message"])

    def test_clipboard_only_still_warns_because_it_is_not_browser_only(self):
        # clipboard can come from local OR browser, so it doesn't prove the
        # extension was connected.
        health = sources.capture_health(sources.summarize_sources(
            rows_with(window=5, keystroke=5, clipboard=3)))
        self.assertTrue(health["warning"])

    def test_browser_sources_present_no_warning(self):
        health = sources.capture_health(sources.summarize_sources(
            rows_with(window=5, keystroke=5, click=2, structured_read=1)))
        self.assertFalse(health["warning"])
        self.assertIsNone(health["message"])

    def test_empty_session_does_not_warn(self):
        health = sources.capture_health(sources.summarize_sources([]))
        self.assertFalse(health["warning"])

    def test_threshold_is_configurable(self):
        # one browser-only event, but require at least two -> warns
        summary = sources.summarize_sources(rows_with(window=5, keystroke=5, click=1))
        self.assertTrue(sources.capture_health(summary, min_browser_events=2)["warning"])
        self.assertFalse(sources.capture_health(summary, min_browser_events=1)["warning"])

    def test_semantic_set_is_the_meaning_carrying_sources(self):
        """What the warning keys off. Deliberately independent of which capturer
        supplied them — the extension and Accessibility both produce all three, so
        asking "was the browser connected?" is the wrong question now."""
        sem = sources.semantic_sources()
        self.assertIn("click", sem)
        self.assertIn("target", sem)
        self.assertIn("structured_read", sem)
        self.assertNotIn("clipboard", sem)   # can't prove either capturer was live
        self.assertNotIn("keystroke", sem)
        self.assertNotIn("window", sem)

    def test_nothing_is_browser_only_any_more(self):
        """The emptiness is the point: Accessibility produces all three semantic
        sources for native apps, so no source type implies the extension."""
        self.assertEqual(sources.browser_only_sources(), set())

    def test_accessibility_alone_satisfies_the_check(self):
        """A Safari/native session with no extension is now a HEALTHY session, as
        long as something captured what the actions meant."""
        rows = rows_with(window=5, keystroke=5, target=4, click=2)
        for r in rows:
            r["payload"] = {"origin": "local"}
            if r["source"] == "window":
                # Titles supplied by AX rather than Screen Recording — the whole
                # point of the fallback, and the other half of a healthy session.
                r["payload"]["title"] = "Add product · Shopify"
        health = sources.capture_health(sources.summarize_sources(rows), rows=rows)
        self.assertFalse(health["warning"])
        self.assertEqual(health["by_origin"], {"local": 16})


if __name__ == "__main__":
    unittest.main()
