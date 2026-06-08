"""Regression B — contiguous same-window data-entry fragments coalesce into one
`enter_data` consistently (regardless of count, position, or chunk seam), while
distinct actions separated by a different action are not coalesced.

Neutral fixtures only; no run-specific values, names, ids, or chunk sizes as
assertions.
"""

import unittest

from trace.reconstruct import chunking, validate


def _mk(rows_spec):
    """rows_spec: ('window', app, title) | ('key', window, text). Returns
    (rows, raw_by_id) with 1-based ids and 1s-spaced timestamps."""
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


def _entry(window, evidence, app="App", detail="typed", conf="high"):
    return {"id": "", "ts_start": evidence[0], "ts_end": evidence[-1], "app": app,
            "window": window, "action": "enter_data", "detail": detail,
            "evidence": list(evidence), "confidence": conf}


class CoalesceContiguousTest(unittest.TestCase):
    def _raw_for(self, n_keys, window="Sheet"):
        spec = [("window", "App", window)] + [("key", window, f"item{i}") for i in range(n_keys)]
        return _mk(spec)

    def test_fragments_collapse_to_one_regardless_of_count(self):
        for n in (2, 4, 7):
            rows, raw_by_id = self._raw_for(n)
            ks = [r["id"] for r in rows if r["source"] == "keystroke"]
            # one fragment per keystroke (maximally fragmented), in order
            events = validate.resequence([_entry("Sheet", [k]) for k in ks])
            merged = validate.coalesce_contiguous(events, raw_by_id)
            entries = [e for e in merged if e["action"] == "enter_data"]
            self.assertEqual(len(entries), 1, f"n={n}")
            self.assertEqual(set(entries[0]["evidence"]), set(ks))

    def test_stable_across_stream_position(self):
        # the same fragmented entry preceded by varying amounts of other content
        for pad in (0, 1, 5):
            spec = [("window", "App", "Other")] + [("key", "Other", f"p{i}") for i in range(pad)]
            spec += [("window", "App", "Sheet")] + [("key", "Sheet", f"x{i}") for i in range(4)]
            rows, raw_by_id = _mk(spec)
            sheet_ks = [r["id"] for r in rows
                        if r["source"] == "keystroke" and r["payload"]["window"] == "Sheet"]
            events = [_entry("Other", [r["id"]], detail="pad")
                      for r in rows if r["source"] == "keystroke" and r["payload"]["window"] == "Other"]
            events += [_entry("Sheet", [k]) for k in sheet_ks]
            events = validate.resequence(events)
            merged = validate.coalesce_contiguous(events, raw_by_id)
            sheet_entries = [e for e in merged if e["window"] == "Sheet"]
            self.assertEqual(len(sheet_entries), 1, f"pad={pad}")
            self.assertEqual(set(sheet_entries[0]["evidence"]), set(sheet_ks))

    def test_distinct_actions_separated_by_different_action_not_coalesced(self):
        rows, raw_by_id = _mk([
            ("window", "App", "Sheet"),
            ("key", "Sheet", "first record"),
            ("window", "App", "Other"),          # a different-window action between
            ("key", "Other", "interlude"),
            ("window", "App", "Sheet"),
            ("key", "Sheet", "second record"),
        ])
        events = validate.resequence([
            _entry("Sheet", ["r0002"], detail="first"),
            _entry("Other", ["r0004"], detail="mid"),
            _entry("Sheet", ["r0006"], detail="second"),
        ])
        merged = validate.coalesce_contiguous(events, raw_by_id)
        sheet_entries = [e for e in merged if e["window"] == "Sheet"]
        self.assertEqual(len(sheet_entries), 2)  # not coalesced across the interlude

    def test_navigations_not_coalesced(self):
        rows, raw_by_id = _mk([("window", "App", "A"), ("window", "App", "A")])
        navs = validate.resequence([
            {"id": "", "ts_start": "r0001", "ts_end": "r0001", "app": "App", "window": "A",
             "action": "navigate", "detail": "to A", "evidence": ["r0001"], "confidence": "high"},
            {"id": "", "ts_start": "r0002", "ts_end": "r0002", "app": "App", "window": "A",
             "action": "navigate", "detail": "to A", "evidence": ["r0002"], "confidence": "high"},
        ])
        merged = validate.coalesce_contiguous(navs, raw_by_id)
        # navigations carry no material evidence -> not coalesced by this pass
        self.assertEqual(len(merged), 2)


class CoalesceAcrossSeamTest(unittest.TestCase):
    """Full stitch+coalesce composition: one contiguous entry, maximally fragmented
    by the model, yields a single enter_data regardless of chunk size — including
    when it straddles a chunk seam."""

    def _build(self, n_keys):
        spec = [("window", "App", "Pre"), ("key", "Pre", "p")]
        spec.append(("window", "App", "Sheet"))
        for i in range(n_keys):
            spec.append(("key", "Sheet", f"x{i}"))
        spec.append(("window", "App", "Done"))
        return _mk(spec)

    def _emit_per_keystroke(self, chunk_rows):
        events, app, win = [], "", None
        for r in chunk_rows:
            if r["source"] == "window":
                app, win = r["payload"]["app"], r["payload"]["title"]
            elif r["source"] == "keystroke":
                w = win if win is not None else r["payload"].get("window", "")
                events.append(_entry(w, [r["id"]], app=app, detail="frag"))
        return events

    def test_one_entry_across_chunk_sizes_and_seams(self):
        for chunk_size, overlap in [(5, 2), (6, 2), (8, 3)]:
            rows, raw_by_id = self._build(n_keys=6)
            sheet_ks = [r["id"] for r in rows
                        if r["source"] == "keystroke" and r["payload"]["window"] == "Sheet"]
            chunks = chunking.chunk(rows, chunk_size, overlap)
            self.assertGreater(len(chunks), 1)
            outputs = [self._emit_per_keystroke(ch) for ch in chunks]
            log = validate.stitch(outputs, raw_by_id, validate.ValidationReport())
            log = validate.coalesce_contiguous(validate.resequence(log), raw_by_id)
            sheet_entries = [e for e in log if e["window"] == "Sheet"]
            msg = f"cs={chunk_size} ov={overlap}"
            self.assertEqual(len(sheet_entries), 1, msg)
            self.assertEqual(set(sheet_entries[0]["evidence"]), set(sheet_ks), msg)


if __name__ == "__main__":
    unittest.main()
