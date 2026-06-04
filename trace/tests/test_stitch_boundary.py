"""Defect 1 — a single action whose evidence straddles a chunk boundary must be
emitted exactly once (as its most complete reconstruction), while two distinct
but similar actions must both survive.

These tests construct neutral fixtures and a neutral stub "model" that emits one
event per contiguous same-window run of keystrokes — mirroring how a chunk-local
reconstruction sees a typing action. No value, id, name, app, chunk size, or
position from any external run appears as a constant here; everything is varied
parametrically.
"""

import unittest

from trace.reconstruct import chunking, validate


def stub_emit_for_chunk(chunk_rows):
    """Reconstruct typing actions from a chunk the way a model would: one event per
    maximal run of keystrokes within the same window (a window change ends a run).
    A run truncated by the chunk's end is emitted as-is (the partial the earlier
    chunk can see)."""
    events, run, app, win = [], [], "", None

    def push():
        if run:
            events.append({
                "action": "enter_data", "app": app, "window": win,
                "evidence": [r["id"] for r in run],
                "detail": "typed " + " ".join(r["payload"].get("text", "") for r in run),
                "confidence": "high",
            })

    for r in chunk_rows:
        if r["source"] == "window":
            push(); run.clear()
            app, win = r["payload"]["app"], r["payload"]["title"]
        elif r["source"] == "keystroke":
            if win is None:
                # window event out of view (chunk began mid-run): app unknown,
                # window from the keystroke's own context — as the real engine does.
                win = r["payload"].get("window", "")
            run.append(r)
    push()
    return events


def _mk(rows_spec):
    """rows_spec: list of ('window', app, title) or ('key', window, text).
    Returns (rows, raw_by_id) with 1-based zero-padded ids and 1s-spaced ts."""
    rows = []
    for i, spec in enumerate(rows_spec):
        rid = f"r{i + 1:04d}"
        ts = f"2026-01-01T00:00:{i:02d}.000"
        if spec[0] == "window":
            rows.append({"id": rid, "ts": ts, "source": "window",
                         "payload": {"app": spec[1], "title": spec[2]}})
        else:
            rows.append({"id": rid, "ts": ts, "source": "keystroke",
                         "payload": {"text": spec[2], "window": spec[1], "ts_end": ts}})
    return rows, {r["id"]: r for r in rows}


def build_straddle(chunk_size, overlap, seam_ordinal):
    """One short typing action in window 'Composer' that straddles the
    ``seam_ordinal``-th chunk seam: it is truncated in the chunk before the seam
    and complete in the chunk that begins at the seam. The action fits within one
    chunk (so a complete view exists), begins inside the overlap, and ends past
    the earlier chunk's cutoff. Geometry is derived from chunk_size/overlap only —
    no literal indices."""
    step = chunk_size - overlap
    run_start = (seam_ordinal + 1) * step       # a chunk begins exactly here
    run_len = overlap + 1                        # >= overlap (straddles), < chunk_size-1 (fits)

    spec = [("window", "FillerApp", "Filler")]
    for i in range(1, run_start):                # leading filler shifts the seam
        spec.append(("key", "Filler", f"f{i}"))
    spec.append(("window", "MailApp", "Composer"))   # at index run_start
    for j in range(run_len):                     # the straddling run
        spec.append(("key", "Composer", f"w{j}"))
    spec.append(("window", "DoneApp", "Done"))
    rows, raw_by_id = _mk(spec)
    composer_ks = [r["id"] for r in rows
                   if r["source"] == "keystroke" and r["payload"]["window"] == "Composer"]
    return rows, raw_by_id, composer_ks


def run_pipeline(rows, raw_by_id, chunk_size, overlap):
    chunks = chunking.chunk(rows, chunk_size, overlap)
    outputs = [stub_emit_for_chunk(ch) for ch in chunks]
    return validate.stitch(outputs, raw_by_id, validate.ValidationReport())


class StraddlePositiveTest(unittest.TestCase):
    def test_straddle_collapses_to_one_complete_event_many_configs(self):
        # three chunk sizes x two straddle positions (action straddles the first
        # seam, then the second). Each must yield exactly one Composer event whose
        # evidence is the *complete* run.
        for chunk_size, overlap in [(6, 2), (8, 3), (10, 3)]:
            for seam_ordinal in (0, 1):
                rows, raw_by_id, composer_ks = build_straddle(
                    chunk_size, overlap, seam_ordinal)
                # sanity: more than one chunk, and the action really straddles
                self.assertGreater(len(chunking.chunk(rows, chunk_size, overlap)), 1)

                log = run_pipeline(rows, raw_by_id, chunk_size, overlap)
                composer = [e for e in log if e["window"] == "Composer"]
                msg = f"chunk_size={chunk_size} overlap={overlap} seam={seam_ordinal}"
                self.assertEqual(len(composer), 1, msg)
                self.assertEqual(set(composer[0]["evidence"]), set(composer_ks), msg)


class DistinctActionsNegativeTest(unittest.TestCase):
    def _two_emails(self):
        # two separate Composer visits (a different window between them) => two
        # distinct typing actions in the same window/action type.
        spec = [
            ("window", "MailApp", "Composer"),
            ("key", "Composer", "to alice body one"),
            ("key", "Composer", "more of one"),
            ("window", "BookApp", "AddressBook"),
            ("key", "AddressBook", "lookup"),
            ("window", "MailApp", "Composer"),
            ("key", "Composer", "to bob body two"),
            ("key", "Composer", "more of two"),
            ("window", "DoneApp", "Done"),
        ]
        return _mk(spec)

    def test_distinct_similar_actions_not_over_merged(self):
        rows, raw_by_id = self._two_emails()
        # both as a single chunk, and split across chunks — both must keep 2
        for chunk_size, overlap in [(len(rows), 0), (4, 2), (5, 1)]:
            log = run_pipeline(rows, raw_by_id, chunk_size, overlap)
            composer = [e for e in log if e["window"] == "Composer"]
            self.assertEqual(len(composer), 2,
                             f"over-merged distinct actions at cs={chunk_size}/ov={overlap}")
            ev0, ev1 = set(composer[0]["evidence"]), set(composer[1]["evidence"])
            self.assertTrue(ev0.isdisjoint(ev1))


class SubsetStillDedupesTest(unittest.TestCase):
    def test_identical_and_subset_same_action_collapse(self):
        rows, raw_by_id = _mk([
            ("window", "App", "Win"),
            ("key", "Win", "alpha"),
            ("key", "Win", "beta"),
        ])
        ev_full = {"action": "enter_data", "app": "App", "window": "Win",
                   "evidence": ["r0002", "r0003"], "detail": "typed alpha beta",
                   "confidence": "high"}
        ev_partial = {"action": "enter_data", "app": "App", "window": "Win",
                      "evidence": ["r0002"], "detail": "typed alpha", "confidence": "medium"}
        log = validate.stitch([[ev_partial], [ev_full]], raw_by_id, validate.ValidationReport())
        self.assertEqual(len(log), 1)
        self.assertEqual(set(log[0]["evidence"]), {"r0002", "r0003"})  # kept the complete one

    def test_navigation_not_absorbed_into_dataentry(self):
        # a window-only navigation must NOT be merged into a data-entry event that
        # shares the window (different action type => structurally distinct).
        rows, raw_by_id = _mk([("window", "App", "Win"), ("key", "Win", "x")])
        nav = {"action": "navigate", "app": "App", "window": "Win",
               "evidence": ["r0001"], "detail": "opened Win", "confidence": "high"}
        entry = {"action": "enter_data", "app": "App", "window": "Win",
                 "evidence": ["r0001", "r0002"], "detail": "typed x", "confidence": "high"}
        log = validate.stitch([[nav, entry]], raw_by_id, validate.ValidationReport())
        actions = sorted(e["action"] for e in log)
        self.assertEqual(actions, ["enter_data", "navigate"])


if __name__ == "__main__":
    unittest.main()
