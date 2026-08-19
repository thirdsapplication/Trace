import unittest

from trace.reconstruct import validate


def raw_index():
    rows = [
        {"id": "r0001", "ts": "2026-06-04T10:00:01.000", "source": "window",
         "payload": {"app": "Mail", "title": "Inbox"}},
        {"id": "r0002", "ts": "2026-06-04T10:00:02.000", "source": "keystroke",
         "payload": {"text": "Acme Corp 12 widgets", "window": "Sheet", "ts_end": "t"}},
        {"id": "r0003", "ts": "2026-06-04T10:00:03.000", "source": "keystroke",
         "payload": {"text": "ship date 2026-06-10", "window": "Sheet", "ts_end": "t"}},
    ]
    return {r["id"]: r for r in rows}


class NormalizeActionTest(unittest.TestCase):
    def test_synonym_mapped(self):
        self.assertEqual(validate.normalize_action("Reply To Email"), ("reply_email", True))

    def test_spaces_and_dashes(self):
        self.assertEqual(validate.normalize_action("enter-order data"), ("enter_data", True))

    def test_unknown_kept_snake_but_flagged_not_in_vocab(self):
        norm, in_vocab = validate.normalize_action("frobnicate_thing")
        self.assertEqual(norm, "frobnicate_thing")
        self.assertFalse(in_vocab)

    def test_empty_defaults_unknown(self):
        self.assertEqual(validate.normalize_action(""), ("unknown", False))


class ValidateEventTest(unittest.TestCase):
    def test_backfills_ts_and_window_from_evidence(self):
        rep = validate.ValidationReport()
        ev = {"action": "enter_data", "detail": "entered order Acme Corp 12 widgets ship date 2026-06-10",
              "evidence": ["r0002", "r0003"], "confidence": "high"}
        out = validate.validate_event(ev, raw_index(), rep)
        self.assertEqual(out["ts_start"], "2026-06-04T10:00:02.000")
        self.assertEqual(out["ts_end"], "2026-06-04T10:00:03.000")
        self.assertEqual(out["window"], "Sheet")

    def test_invalid_confidence_repaired(self):
        rep = validate.ValidationReport()
        ev = {"action": "read_email", "detail": "read", "evidence": ["r0001"], "confidence": "certain"}
        out = validate.validate_event(ev, raw_index(), rep)
        self.assertEqual(out["confidence"], "low")
        self.assertTrue(rep.errors)

    def test_unknown_evidence_dropped_and_flagged(self):
        rep = validate.ValidationReport()
        ev = {"action": "read_email", "detail": "read", "evidence": ["r9999"], "confidence": "high"}
        out = validate.validate_event(ev, raw_index(), rep)
        self.assertEqual(out["evidence"], [])
        self.assertTrue(rep.errors)

    def test_short_evidence_id_normalized(self):
        rep = validate.ValidationReport()
        ev = {"action": "read_email", "detail": "read", "evidence": ["r1"], "confidence": "high"}
        out = validate.validate_event(ev, raw_index(), rep)
        self.assertEqual(out["evidence"], ["r0001"])

    def test_empty_detail_dropped(self):
        rep = validate.ValidationReport()
        self.assertIsNone(validate.validate_event(
            {"action": "x", "detail": "", "evidence": [], "confidence": "low"}, raw_index(), rep))

    def test_fragmentation_flagged_when_typed_text_missing(self):
        rep = validate.ValidationReport()
        ev = {"action": "enter_data", "detail": "did something",
              "evidence": ["r0002", "r0003"], "confidence": "high"}
        validate.validate_event(ev, raw_index(), rep)
        self.assertTrue(rep.flags, "expected a fragmentation review flag")

    def test_fragmentation_not_flagged_when_values_preserved(self):
        rep = validate.ValidationReport()
        ev = {"action": "enter_data",
              "detail": "entered Acme Corp 12 widgets with ship date 2026-06-10",
              "evidence": ["r0002", "r0003"], "confidence": "high"}
        validate.validate_event(ev, raw_index(), rep)
        self.assertFalse(rep.flags)


class StitchTest(unittest.TestCase):
    def test_dedupes_overlap_duplicate_and_renumbers(self):
        idx = raw_index()
        rep = validate.ValidationReport()
        chunk_a = [{"action": "read_email", "detail": "read inbox", "evidence": ["r0001"], "confidence": "high"},
                   {"action": "enter_data", "detail": "entered Acme Corp 12 widgets ship date 2026-06-10",
                    "evidence": ["r0002", "r0003"], "confidence": "high"}]
        # chunk_b re-emits the same enter_data event (overlap region)
        chunk_b = [{"action": "enter_data", "detail": "entered Acme Corp 12 widgets ship date 2026-06-10",
                    "evidence": ["r0002", "r0003"], "confidence": "high"}]
        log = validate.stitch([chunk_a, chunk_b], idx, rep)
        self.assertEqual(len(log), 2)
        self.assertEqual([e["id"] for e in log], ["e001", "e002"])

    def test_ordered_by_ts_start(self):
        idx = raw_index()
        rep = validate.ValidationReport()
        chunk = [{"action": "enter_data", "detail": "second Acme Corp 12 widgets ship date 2026-06-10",
                  "evidence": ["r0003"], "confidence": "high"},
                 {"action": "read_email", "detail": "first", "evidence": ["r0001"], "confidence": "high"}]
        log = validate.stitch([chunk], idx, rep)
        self.assertEqual(log[0]["action"], "read_email")  # r0001 is earliest


class AccessibilityEvidenceTest(unittest.TestCase):
    """Regression guard for the breakage Accessibility capture would otherwise
    introduce silently.

    ``target`` is classified as navigation CONTEXT, which was correct while the
    only producer was the browser extension: a target there rides alongside the
    click that carries the action. Accessibility capture breaks that assumption —
    in a native app, or a workflow that is mostly button-pushing, a value the
    worker selected can arrive as a target and NOTHING else. If those stay
    "context" they contribute no material span, and two chunk-overlap views of one
    action stop being recognisable as the same action. Nothing raises; the event
    log just quietly fills with duplicates.
    """

    def _raw(self, changed=True):
        rows = [
            {"id": "r0010", "ts": "2026-08-19T10:00:10.000", "source": "target",
             "payload": {"role": "combobox", "label": "Status", "value": "Active",
                         "window": "Add product · Shopify", "origin": "local"}},
            {"id": "r0011", "ts": "2026-08-19T10:00:11.000", "source": "target",
             "payload": {"role": "combobox", "label": "Vendor", "value": "Acme",
                         "window": "Add product · Shopify", "origin": "local"}},
            {"id": "r0012", "ts": "2026-08-19T10:00:12.000", "source": "target",
             "payload": {"role": "textbox", "label": "Price", "value": "24.99",
                         "window": "Add product · Shopify", "origin": "local"}},
        ]
        if changed:
            for r in rows:
                r["payload"]["changed"] = True
        return {r["id"]: r for r in rows}

    def _view(self, evidence):
        return {"action": "enter_data", "detail": "set the product's fields",
                "evidence": evidence, "confidence": "high"}

    def test_two_chunk_views_of_one_ax_action_collapse_to_one(self):
        """Neither evidence set contains the other, so only the material-span
        overlap can catch this. Before value-changing targets counted as material
        it produced two events."""
        rep = validate.ValidationReport()
        out = validate.stitch([[self._view(["r0010", "r0011"])],
                               [self._view(["r0011", "r0012"])]], self._raw(), rep)
        self.assertEqual(len(out), 1)
        # Equally complete views tie, so the incumbent is kept — the point here is
        # that the second was recognised as the same action at all.
        self.assertEqual(out[0]["evidence"], ["r0010", "r0011"])

    def test_a_focus_move_stays_context_and_does_not_merge_distinct_actions(self):
        """The other half: a bare focus change is still navigation, so two
        genuinely distinct actions that merely passed through the same element
        must survive as two."""
        rep = validate.ValidationReport()
        out = validate.stitch([[self._view(["r0010", "r0011"])],
                               [self._view(["r0011", "r0012"])]],
                              self._raw(changed=False), rep)
        self.assertEqual(len(out), 2)

    def test_window_backfills_from_an_ax_target(self):
        """window is the strong identifier _same_action dedupes on. Backfilling it
        used to look only at window/keystroke events, so an AX-only action got an
        empty window and became indistinguishable from any other."""
        rep = validate.ValidationReport()
        out = validate.stitch([[self._view(["r0010"])]], self._raw(), rep)
        self.assertEqual(out[0]["window"], "Add product · Shopify")


if __name__ == "__main__":
    unittest.main()
