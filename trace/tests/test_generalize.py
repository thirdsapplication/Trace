"""Gap #3 B3: the generalization/scrub pass + the Voyage embeddings client's graceful
degradation and happy path (network mocked). Offline, no platform."""

import io
import json
import os
import unittest
from unittest import mock

from trace.agent import embeddings, generalize


def _fixed(out):
    return lambda system, user: out


class GeneralizeTest(unittest.TestCase):
    def test_reusable_lesson_parsed(self):
        mf = _fixed('{"reusable": true, "lesson": "When an order lacks a ship date, ask the customer."}')
        r = generalize.generalize_decision(
            {"question": "no ship date?", "answer": "ask the buyer",
             "context": {"context": "PO omits date"}}, mf)
        self.assertTrue(r["reusable"])
        self.assertIn("ship date", r["lesson"])

    def test_non_reusable(self):
        r = generalize.generalize_decision(
            {"question": "q", "answer": "a"}, _fixed('{"reusable": false, "lesson": null}'))
        self.assertFalse(r["reusable"])
        self.assertIsNone(r["lesson"])

    def test_reusable_true_but_empty_lesson_is_not_reusable(self):
        r = generalize.generalize_decision(
            {"question": "q", "answer": "a"}, _fixed('{"reusable": true, "lesson": ""}'))
        self.assertFalse(r["reusable"])

    def test_bad_json_best_effort(self):
        r = generalize.generalize_decision({"question": "q", "answer": "a"}, _fixed("not json"))
        self.assertEqual(r, {"reusable": False, "lesson": None})

    def test_missing_question_or_answer_skips_model(self):
        mf = _fixed('{"reusable": true, "lesson": "x"}')
        self.assertFalse(generalize.generalize_decision({"question": "", "answer": "a"}, mf)["reusable"])
        self.assertFalse(generalize.generalize_decision({"question": "q", "answer": ""}, mf)["reusable"])

    def test_model_error_best_effort(self):
        def boom(system, user):
            raise RuntimeError("model down")
        self.assertEqual(generalize.generalize_decision({"question": "q", "answer": "a"}, boom),
                         {"reusable": False, "lesson": None})

    def test_verify_lesson_scrub(self):
        self.assertTrue(generalize.verify_lesson_scrub("A general rule.", _fixed('{"safe": true}')))
        self.assertFalse(generalize.verify_lesson_scrub("Email bob@x.com.", _fixed('{"safe": false}')))
        self.assertFalse(generalize.verify_lesson_scrub("", _fixed('{"safe": true}')))  # empty
        self.assertFalse(generalize.verify_lesson_scrub(                                  # fails closed
            "x", lambda s, u: (_ for _ in ()).throw(RuntimeError())))


class EmbeddingsTest(unittest.TestCase):
    def test_no_key_returns_none(self):
        old = os.environ.pop("VOYAGE_API_KEY", None)
        try:
            self.assertIsNone(embeddings.embed("hello"))
            self.assertFalse(embeddings.is_configured())
        finally:
            if old is not None:
                os.environ["VOYAGE_API_KEY"] = old

    def test_empty_input_returns_none(self):
        self.assertIsNone(embeddings.embed([], api_key="k"))
        self.assertIsNone(embeddings.embed(["  ", ""], api_key="k"))

    def test_happy_path_mocked(self):
        payload = json.dumps({"data": [{"embedding": [0.1] * embeddings.EMBED_DIM}]}).encode()
        with mock.patch("trace.agent.embeddings.urllib.request.urlopen") as uo:
            uo.return_value = io.BytesIO(payload)          # supports context manager + read()
            out = embeddings.embed("a lesson", api_key="k")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), embeddings.EMBED_DIM)

    def test_api_error_returns_none(self):
        with mock.patch("trace.agent.embeddings.urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(embeddings.embed("x", api_key="k"))


if __name__ == "__main__":
    unittest.main()
