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


def _chunk_first_id(user):
    """The first raw id in a chunk's stream (the summary above it carries no ids)."""
    stream = user.split("RAW CAPTURE STREAM", 1)[1]
    return stream.split("[", 1)[1].split(" @", 1)[0]


class ConcurrentChunksTest(unittest.TestCase):
    """Chunks go to the model at once; the log must not depend on which finishes first."""

    def _one_event_per_chunk(self, delays=None, fail_on=None):
        import threading
        import time
        seen, lock = [], threading.Lock()

        def model_fn(system, user):
            first = _chunk_first_id(user)
            with lock:
                seen.append(user)
            if delays:
                time.sleep(delays.get(first, 0))
            if first == fail_on:
                raise RuntimeError("rate limited")
            return json.dumps([{"action": "navigate", "detail": f"chunk at {first}",
                                "evidence": [first], "confidence": "high"}])
        return model_fn, seen

    def test_out_of_order_finishes_still_build_an_ordered_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            write_raw(p, 10)   # chunk_size 4, overlap 1 -> chunks start r0001, r0004, r0007
            # the first chunk finishes last
            model_fn, _ = self._one_event_per_chunk(delays={"r0001": 0.2})
            progress = []
            log, _ = reconstruct(p, chunk_size=4, overlap=1, model_fn=model_fn,
                                 workers=3, progress_cb=lambda a, b: progress.append((a, b)))
            self.assertEqual([e["evidence"] for e in log], [["r0001"], ["r0004"], ["r0007"]])
            self.assertEqual(progress, [(0, 3), (1, 3), (2, 3), (3, 3)])

    def test_concurrent_and_sequential_agree_when_the_model_is_stateless(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            write_raw(p, 10)
            model_fn, _ = self._one_event_per_chunk()
            seq, _ = reconstruct(p, chunk_size=4, overlap=1, model_fn=model_fn, workers=1)
            par, _ = reconstruct(p, chunk_size=4, overlap=1, model_fn=model_fn, workers=4)
            self.assertEqual(seq, par)

    def test_the_summary_describes_what_came_before_without_citable_ids(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            write_raw(p, 10)
            model_fn, seen = self._one_event_per_chunk()
            reconstruct(p, chunk_size=4, overlap=1, model_fn=model_fn, workers=3)
            later = next(u for u in seen if _chunk_first_id(u) == "r0007")
            summary = later.split("RAW CAPTURE STREAM", 1)[0]
            self.assertIn('title="window-6"', summary)   # the raw events before the chunk
            self.assertNotIn("r000", summary)              # ...but nothing to cite
            first = next(u for u in seen if _chunk_first_id(u) == "r0001")
            self.assertIn("(none yet)", first)

    def test_one_failing_chunk_fails_the_reconstruction(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            write_raw(p, 10)
            model_fn, _ = self._one_event_per_chunk(fail_on="r0004")
            with self.assertRaises(RuntimeError):
                reconstruct(p, chunk_size=4, overlap=1, model_fn=model_fn, workers=3)

    def test_no_more_chunks_run_at_once_than_workers(self):
        import threading
        import time
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            write_raw(p, 30)
            lock, live, peak = threading.Lock(), [0], [0]

            def model_fn(system, user):
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                time.sleep(0.05)
                with lock:
                    live[0] -= 1
                return "[]"
            reconstruct(p, chunk_size=4, overlap=1, model_fn=model_fn, workers=2)
            self.assertEqual(peak[0], 2)

    def test_one_worker_keeps_the_model_output_summary(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.jsonl")
            write_raw(p, 10)
            model_fn, seen = self._one_event_per_chunk()
            reconstruct(p, chunk_size=4, overlap=1, model_fn=model_fn, workers=1)
            self.assertIn("chunk at r0001", seen[1])       # chunk 2 saw chunk 1's reading


if __name__ == "__main__":
    unittest.main()
