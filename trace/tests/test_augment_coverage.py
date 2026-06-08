"""Regression A — document/window-focus opens are emitted at most once and only
for a SUSTAINED, otherwise-UNREPRESENTED focus; immediate-action windows and
blank/transient titles produce none; one action vocabulary for all documents.

Fixtures vary app names and "file types" and use only constructed values; no app
or extension literal drives any logic or assertion.
"""

import datetime
import unittest

from trace.reconstruct import augment, validate

_BASE = datetime.datetime(2026, 1, 1, 9, 0, 0)


def _raw(seq):
    """seq items: ('window', app, title, dwell_s) | ('key', window, text, gap_s)."""
    rows, t = [], 0.0
    for i, item in enumerate(seq):
        rid = f"r{i + 1:04d}"
        ts = (_BASE + datetime.timedelta(seconds=t)).isoformat(timespec="milliseconds")
        if item[0] == "window":
            rows.append({"id": rid, "ts": ts, "source": "window",
                         "payload": {"app": item[1], "title": item[2]}})
            t += item[3]
        else:
            rows.append({"id": rid, "ts": ts, "source": "keystroke",
                         "payload": {"text": item[2], "window": item[1], "ts_end": ts}})
            t += item[3]
    return rows


def _opens(out):
    return [e for e in out if e["action"] == augment.OPEN_ACTION]


class NoRedundantOpenTest(unittest.TestCase):
    def test_immediate_action_windows_across_apps_emit_no_open(self):
        # several different "apps"/"file types", each focused and immediately acted
        # in (keystrokes within the focus span) -> the action represents the open.
        scenarios = [
            ("EditorOne", "alpha.aaa", "Alpha"),
            ("ToolTwo", "beta.bbb", "Beta"),
            ("AppThree", "gamma.ccc", "Gamma"),
        ]
        for app, title, win in scenarios:
            rows = _raw([
                ("window", app, title, 30),     # long focus...
                ("key", title, "did work here", 1),   # ...but acted upon
                ("window", "Next", "next", 1),
            ])
            # an existing action represents the focus (cites the keystroke in span)
            existing = [{"id": "e001", "ts_start": "t", "ts_end": "t", "app": app,
                         "window": title, "action": "enter_data", "detail": "worked",
                         "evidence": ["r0001", "r0002"], "confidence": "high"}]
            out = augment.augment_document_opens(existing, rows, persistence_seconds=3.0)
            self.assertEqual(_opens(out), [], f"redundant open for {app}")

    def test_navigation_only_event_is_not_double_opened(self):
        rows = _raw([("window", "Br", "A Page", 30), ("window", "X", "x", 1)])
        existing = [{"id": "e001", "ts_start": "t", "ts_end": "t", "app": "Br",
                     "window": "A Page", "action": "navigate", "detail": "went there",
                     "evidence": ["r0001"], "confidence": "high"}]
        self.assertEqual(_opens(augment.augment_document_opens(existing, rows, 3.0)), [])

    def test_incidental_citation_elsewhere_still_opens(self):
        # native doc consulted, then worker types in a *different* window; the entry
        # cites the doc's window row incidentally -> doc is still surfaced.
        rows = _raw([
            ("window", "Viewer", "ref.zzz", 30),   # sustained, no action inside
            ("window", "Sheet", "Book", 5),
            ("key", "Book", "typed elsewhere", 1),
        ])
        existing = [{"id": "e001", "ts_start": "t", "ts_end": "t", "app": "Sheet",
                     "window": "Book", "action": "enter_data", "detail": "entered",
                     "evidence": ["r0001", "r0003"], "confidence": "high"}]
        opens = _opens(augment.augment_document_opens(existing, rows, 3.0))
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["window"], "ref.zzz")


class SustainedOpenAcrossAppsTest(unittest.TestCase):
    def test_sustained_reference_open_one_per_app_same_vocab(self):
        # several different apps/file types, each a sustained consult with no action
        rows = _raw([
            ("window", "ReaderA", "doc.aaa", 30),
            ("window", "ReaderB", "sheet.bbb", 30),
            ("window", "ReaderC", "page.ccc", 30),
            ("window", "End", "end", 1),
        ])
        opens = _opens(augment.augment_document_opens([], rows, 3.0))
        self.assertEqual({o["window"] for o in opens}, {"doc.aaa", "sheet.bbb", "page.ccc"})
        # one action vocabulary regardless of app/file type
        self.assertEqual({o["action"] for o in opens}, {augment.OPEN_ACTION})
        for o in opens:                       # grounded in the title only; one window row each
            self.assertEqual(len(o["evidence"]), 1)
            self.assertIn(o["window"], o["detail"])


class TransientAndBlankTest(unittest.TestCase):
    def test_blank_or_anonymous_title_emits_no_open(self):
        rows = _raw([("window", "App", "   ", 30), ("window", "App", "", 30),
                     ("window", "End", "end", 1)])
        self.assertEqual(_opens(augment.augment_document_opens([], rows, 3.0)), [])

    def test_transient_focus_below_threshold_emits_no_open(self):
        seq = []
        for _ in range(3):
            seq += [("window", "A", "WinA", 1), ("window", "B", "WinB", 1)]
        seq.append(("window", "C", "WinC", 0))
        self.assertEqual(_opens(augment.augment_document_opens([], _raw(seq), 3.0)), [])


class OneVocabularyNormalizationTest(unittest.TestCase):
    def test_format_specific_verbs_normalize_to_one_document_action(self):
        for verb in ("view_pdf", "open_pdf", "read_pdf", "read_document", "view_document"):
            norm, in_vocab = validate.normalize_action(verb)
            self.assertEqual(norm, "open_document", verb)
            self.assertTrue(in_vocab, verb)

    def test_format_specific_verb_not_in_vocab(self):
        from trace.reconstruct.prompt import ACTION_VOCAB
        self.assertNotIn("view_pdf", ACTION_VOCAB)
        self.assertIn("open_document", ACTION_VOCAB)


if __name__ == "__main__":
    unittest.main()
