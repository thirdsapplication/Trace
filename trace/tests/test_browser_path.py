"""Item C — the browser-capture path end to end (collector side).

Verifies the links we can exercise without a live browser:
  link 2/3: the collector receives browser-sourced events on its endpoint and
            persists them into the raw stream in the SAME {ts, source, payload}
            schema as local events, so reconstruction consumes them;
  link 4:   a presence heartbeat (/hello) makes "extension connected" observable
            before the first event;
and that the Item-3 warning genuinely flips: it does NOT fire when browser events
are present, and STILL fires (unchanged) when they are absent.
"""

import json
import threading
import tempfile
import os
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from trace.capture import collector, sources
from trace.capture.raw_writer import RawWriter
from trace.reconstruct import chunking
from trace.reconstruct.reconstruct import reconstruct as reconstruct_fn


class CollectorBrowserPathTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw_path = os.path.join(self._tmp.name, "raw_events.jsonl")
        self.writer = RawWriter(self.raw_path, reset=True)
        self.stats = collector.CollectorStats()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         collector.make_handler(self.writer, self.stats))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self._tmp.cleanup()

    def _post(self, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else b""
        r = urllib.request.Request(url, data=data, method="POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"null")

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
            return resp.status, json.loads(resp.read() or b"null")

    def test_browser_events_persist_in_raw_stream_schema(self):
        batch = {"events": [
            {"source": "click", "payload": {"x": 10, "y": 20, "button": "left",
                                            "target": {"role": "button", "label": "Send", "value": ""}}},
            {"source": "target", "payload": {"role": "combobox", "label": "Status", "value": "Open"}},
            {"source": "structured_read", "payload": {"url": "https://x", "anchor": "click",
                                                      "elements": [{"role": "option", "label": "A", "value": ""}]}},
        ]}
        code, data = self._post("/event", batch)
        self.assertEqual(code, 200)
        self.assertEqual(data["written"], 3)

        rows = chunking.load_raw(self.raw_path)
        self.assertEqual([r["source"] for r in rows], ["click", "target", "structured_read"])
        for r in rows:  # same normalized schema as local events
            self.assertIn("ts", r)
            self.assertIn("payload", r)

    def test_events_flow_through_reconstruction(self):
        self._post("/event", {"events": [
            {"source": "click", "payload": {"x": 1, "y": 2, "button": "left",
                                            "target": {"role": "button", "label": "Save", "value": ""}}},
            {"source": "structured_read", "payload": {"anchor": "page_load",
                                                      "elements": [{"role": "heading", "label": "Title", "value": "Hi"}]}},
        ]})
        captured = {}

        def stub(system, user):
            captured["user"] = user
            return "[]"

        reconstruct_fn(self.raw_path, model_fn=stub)
        # the browser-sourced rows were rendered into the model input
        self.assertIn("CLICK", captured["user"])
        self.assertIn("READ", captured["user"])

    def test_hello_marks_connected_before_any_event(self):
        code, _ = self._post("/hello")
        self.assertEqual(code, 200)
        _, health = self._get("/health")
        self.assertTrue(health["connected"])     # connected via heartbeat
        self.assertFalse(health["detected"])      # but no events yet

    def test_health_reports_detected_after_events(self):
        self._post("/event", {"events": [
            {"source": "click", "payload": {"x": 1, "y": 1, "button": "left", "target": None}}]})
        _, health = self._get("/health")
        self.assertTrue(health["detected"])
        self.assertEqual(health["browser_event_count"], 1)


class WarningFlipsTest(unittest.TestCase):
    """The Item-3 warning must be genuinely condition-driven, not weakened."""

    def test_no_warning_when_browser_events_present(self):
        rows = ([{"source": "window", "payload": {}}] * 5
                + [{"source": "keystroke", "payload": {}}] * 5
                + [{"source": "click", "payload": {}},
                   {"source": "structured_read", "payload": {}}])
        health = sources.capture_health(sources.summarize_sources(rows))
        self.assertFalse(health["warning"])

    def test_warning_still_fires_when_browser_absent(self):
        rows = [{"source": "window", "payload": {}}] * 5 + [{"source": "keystroke", "payload": {}}] * 5
        health = sources.capture_health(sources.summarize_sources(rows))
        self.assertTrue(health["warning"])
        self.assertIsNotNone(health["message"])


if __name__ == "__main__":
    unittest.main()
