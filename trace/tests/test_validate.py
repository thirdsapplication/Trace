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


if __name__ == "__main__":
    unittest.main()
