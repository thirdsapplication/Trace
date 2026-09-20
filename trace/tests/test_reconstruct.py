import json
import os
import tempfile
import unittest

from trace.reconstruct import DEFAULT_MODEL
from trace.reconstruct.reconstruct import _parse_json_array, reconstruct


class ParseTest(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(_parse_json_array('[{"a":1}]'), [{"a": 1}])

    def test_strips_markdown_fence(self):
        self.assertEqual(_parse_json_array('```json\n[{"a":1}]\n```'), [{"a": 1}])

    def test_extracts_array_from_prose(self):
        self.assertEqual(_parse_json_array('Sure! [{"a":1}] done'), [{"a": 1}])

    def test_tolerates_events_wrapper(self):
        self.assertEqual(_parse_json_array('{"events":[{"a":1}]}'), [{"a": 1}])

    def test_garbage_returns_empty(self):
        self.assertEqual(_parse_json_array("not json"), [])


def write_raw(path, n):
    with open(path, "w") as f:
        for i in range(1, n + 1):
            f.write(json.dumps({
                "ts": f"2026-06-04T10:00:{i:02d}.000",
                "source": "window",
                "payload": {"app": "App", "title": f"window-{i}"},
            }) + "\n")


class ReconstructPipelineTest(unittest.TestCase):
    def test_end_to_end_with_mock_model_dedupes_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            write_raw(p, 6)  # -> chunk_size 4, overlap 2 => 2 chunks; overlap r0003,r0004

            def model_fn(system, user):
                # Chunk 1 has r0001; chunk 2 starts at r0005.
                if "r0001" in user:
                    return json.dumps([
                        {"action": "navigate", "detail": "opened window-1",
                         "evidence": ["r0001"], "confidence": "high"},
                        {"action": "navigate", "detail": "opened window-3 then 4",
                         "evidence": ["r0003", "r0004"], "confidence": "high"},
                    ])
                # chunk 2 re-emits the overlap event + a new one
                return json.dumps([
                    {"action": "navigate", "detail": "opened window-3 then 4",
                     "evidence": ["r0003", "r0004"], "confidence": "high"},
                    {"action": "navigate", "detail": "opened window-5",
                     "evidence": ["r0005"], "confidence": "high"},
                ])

            log, report = reconstruct(p, chunk_size=4, overlap=2, model_fn=model_fn)
            # overlap duplicate (r0003,r0004) should be deduped -> 3 events
            self.assertEqual(len(log), 3)
            self.assertEqual([e["id"] for e in log], ["e001", "e002", "e003"])
            # ordered by ts; ids backfilled from evidence
            self.assertEqual(log[0]["evidence"], ["r0001"])
            self.assertEqual(log[0]["app"], "App")

    def test_a_reply_that_parses_to_nothing_is_reported_as_a_lost_chunk(self):
        """A chunk whose reply is not a JSON event list used to vanish silently; the
        report now says which chunk, so an empty log has a visible cause."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            write_raw(p, 6)
            log, report = reconstruct(p, chunk_size=4, overlap=2,
                                      model_fn=lambda s, u: "Sorry, I could not do that.")
            self.assertEqual(log, [])
            self.assertEqual(len(report.errors), 2)                 # one per chunk
            self.assertIn("chunk 1/2", report.errors[0])
            self.assertIn("events were lost", report.errors[0])
            # a genuinely empty reply is not a lost chunk
            _, quiet = reconstruct(p, chunk_size=4, overlap=2, model_fn=lambda s, u: "[]")
            self.assertEqual(quiet.errors, [])

    def test_empty_stream(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            open(p, "w").close()
            log, report = reconstruct(p, model_fn=lambda s, u: "[]")
            self.assertEqual(log, [])

    def test_determinism_same_input_same_output(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            write_raw(p, 6)

            def model_fn(system, user):
                if "r0001" in user:
                    return json.dumps([{"action": "navigate", "detail": "opened window-1",
                                        "evidence": ["r0001"], "confidence": "high"}])
                return json.dumps([{"action": "navigate", "detail": "opened window-5",
                                    "evidence": ["r0005"], "confidence": "high"}])

            a, _ = reconstruct(p, chunk_size=4, overlap=2, model_fn=model_fn)
            b, _ = reconstruct(p, chunk_size=4, overlap=2, model_fn=model_fn)
            self.assertEqual(a, b)

    def test_module_exports_default_model(self):
        self.assertEqual(DEFAULT_MODEL, "claude-fable-5-1")

    def test_optional_target_is_kept_when_written_and_absent_otherwise(self):
        """Prompt Library v2 added ``target`` to the contract as OPTIONAL: kept verbatim
        when the model wrote one (``"unknown"`` included — that is the signal Discovery
        carries forward), and absent, not blanked, when it did not, so an older log or
        model stays byte-identical on the frozen shape."""
        from trace.reconstruct import validate
        raw = {"r0001": {"id": "r0001", "ts": "t", "source": "window",
                         "payload": {"app": "App", "title": "W"}}}
        rep = validate.ValidationReport()
        with_target = validate.validate_event(
            {"action": "enter_data", "detail": "typed a value", "evidence": ["r0001"],
             "confidence": "medium", "target": "unknown"}, raw, rep)
        self.assertEqual(with_target["target"], "unknown")
        without = validate.validate_event(
            {"action": "enter_data", "detail": "typed a value", "evidence": ["r0001"],
             "confidence": "medium"}, raw, rep)
        self.assertNotIn("target", without)
        blank = validate.validate_event(
            {"action": "enter_data", "detail": "typed a value", "evidence": ["r0001"],
             "confidence": "medium", "target": "  "}, raw, rep)
        self.assertNotIn("target", blank)

    def test_prompt_opens_with_the_foundation_and_names_the_real_signals(self):
        from trace.agent.foundation import FOUNDATION
        from trace.reconstruct.prompt import SYSTEM_PROMPT, ACTION_VOCAB
        self.assertTrue(SYSTEM_PROMPT.startswith(FOUNDATION))
        for line in ("WINDOW", "TYPED", "CLICK", "FOCUS", "CLIPBOARD", "READ"):
            self.assertIn(line, SYSTEM_PROMPT)          # the six rendered signal types
        self.assertNotIn("screenshot", SYSTEM_PROMPT.split("## Your job")[1].lower())
        self.assertIn("[REDACTED]", SYSTEM_PROMPT)      # the real redaction token
        for verb in ACTION_VOCAB:
            self.assertIn(verb, SYSTEM_PROMPT)          # the controlled vocabulary survives


if __name__ == "__main__":
    unittest.main()
