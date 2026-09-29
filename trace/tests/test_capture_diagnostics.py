"""Capture diagnostics — a recording made on a machine we cannot see arrives with
enough to diagnose it: machine facts, capture errors that were swallowed, and the
node's log tail. And it is deleted with the recording.
"""

import json
import os
import tempfile
import unittest

from trace.capture import diag
from trace.tests import test_ui_desktop as _desktop  # module import: pytest must not re-collect it
from trace.ui import sessions


class DiagTest(unittest.TestCase):
    def setUp(self):
        diag.reset()

    def test_a_repeating_failure_is_counted_once_with_its_first_message(self):
        for i in range(50):
            try:
                raise OSError(f"element gone {i}")
            except OSError as e:
                diag.note("focus_poll", e)
        errs, dropped = diag.errors()
        self.assertEqual(len(errs), 1)
        self.assertEqual((errs[0]["where"], errs[0]["count"]), ("focus_poll", 50))
        self.assertEqual(errs[0]["message"], "element gone 0")
        self.assertEqual(dropped, 0)

    def test_distinct_kinds_are_capped(self):
        for i in range(diag.MAX_KINDS + 5):
            diag.note(f"site{i}", ValueError("x"))
        errs, dropped = diag.errors()
        self.assertEqual((len(errs), dropped), (diag.MAX_KINDS, 5))

    def test_the_report_carries_machine_facts_and_is_json(self):
        diag.fact("adapter_available", True)
        report = diag.report(capture_status={"running": False})
        self.assertIn("os", report["machine"])
        self.assertTrue(report["facts"]["adapter_available"])
        json.dumps(report)

    def test_the_log_tail_keeps_diagnosis_lines_only(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as f:
            f.write('127.0.0.1 - - "GET /app HTTP/1.1" 200 -\n')
            f.write("[shell +0.1s] spawn attempt 1/3\n")
            f.write("Traceback (most recent call last):\n")
        try:
            tail = diag.log_tail(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(tail, ["[shell +0.1s] spawn attempt 1/3",
                                "Traceback (most recent call last):"])

    def test_a_missing_log_is_an_empty_tail(self):
        self.assertEqual(diag.log_tail("/nonexistent/trace-node.log"), [])


class UploadedDiagnosticsTest(_desktop.DesktopProxyTest):
    """Through the real desktop → cloud upload path."""

    def test_a_desktop_recording_arrives_with_its_diagnostics(self):
        _, _, cookie = self.desk("POST", "/api/auth/signup",
                                 {"email": "diag@x.com", "password": "password1"})
        self.desk("POST", "/api/u/capture/start", cookie=cookie)
        _, stopped, _ = self.desk("POST", "/api/u/capture/stop", cookie=cookie)
        cloud_path = sessions.path(self._cloud_tmp.name, stopped["session_id"],
                                   sessions.DIAGNOSTICS)
        with open(cloud_path, encoding="utf-8") as f:
            report = json.load(f)
        self.assertIn("machine", report)
        self.assertIn("errors", report)
        # a copy stays with the local recording too
        self.assertTrue(os.path.exists(sessions.path(
            self._desk_tmp.name, stopped["local_session_id"], sessions.DIAGNOSTICS)))

    def test_an_upload_without_diagnostics_still_works(self):
        _, _, cookie = self.cloud("POST", "/api/auth/signup",
                                  {"email": "old@x.com", "password": "password1"})
        raw = json.dumps({"ts": "t", "source": "click", "payload": {}}) + "\n"
        status, data, _ = self.cloud("POST", "/api/u/sessions/raw-upload",
                                     {"raw": raw}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(sessions.path(
            self._cloud_tmp.name, data["session_id"], sessions.DIAGNOSTICS)))

    def test_an_oversized_report_is_dropped_not_fatal(self):
        _, _, cookie = self.cloud("POST", "/api/auth/signup",
                                  {"email": "big@x.com", "password": "password1"})
        raw = json.dumps({"ts": "t", "source": "click", "payload": {}}) + "\n"
        huge = {"log_tail": ["x" * 1000] * 400}
        status, data, _ = self.cloud("POST", "/api/u/sessions/raw-upload",
                                     {"raw": raw, "diagnostics": huge}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(sessions.path(
            self._cloud_tmp.name, data["session_id"], sessions.DIAGNOSTICS)))


# Only the tests defined here, not the inherited DesktopProxyTest suite again.
for _name in [n for n in dir(_desktop.DesktopProxyTest) if n.startswith("test_")]:
    if _name not in UploadedDiagnosticsTest.__dict__:
        setattr(UploadedDiagnosticsTest, _name, None)


if __name__ == "__main__":
    unittest.main()
