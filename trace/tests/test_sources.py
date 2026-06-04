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

    def test_browser_only_set_excludes_clipboard(self):
        bo = sources.browser_only_sources()
        self.assertIn("click", bo)
        self.assertIn("target", bo)
        self.assertIn("structured_read", bo)
        self.assertNotIn("clipboard", bo)
        self.assertNotIn("keystroke", bo)


if __name__ == "__main__":
    unittest.main()
