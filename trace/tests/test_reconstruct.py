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
        self.assertEqual(DEFAULT_MODEL, "claude-haiku-4-5")


if __name__ == "__main__":
    unittest.main()
