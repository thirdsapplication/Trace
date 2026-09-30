"""Static guards for the front end, which has no test harness of its own.

A ReferenceError in renderProfile — `mins` declared inside `if (statRow)` and read
after it — threw on EVERY successful load, after the stat tiles were painted and
before the session log, Recent runs and Permissions were. Those three sat on
"Loading…" for good, and because every renderProfile call is fire-and-forget on an
async function the throw surfaced only as an unhandled rejection in the console. It
looked exactly like a server that never answered, and was debugged as one twice.

These are cheap structural pins in the spirit of the prompt-pin tests: they cannot
prove the UI works, but they fail loudly if the specific shape of that bug — or the
absence of the nets that would have made it visible — comes back.
"""

import os
import re
import shutil
import subprocess
import unittest

APP_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "ui", "app_static", "app.js")


def _source():
    with open(APP_JS, encoding="utf-8") as f:
        return f.read()


def _render_profile_body(src):
    """The text of renderProfile, from its declaration to the legacy alias below it."""
    start = src.index("async function renderProfile(")
    end = src.index("const renderActivity = renderProfile;")
    return src[start:end]


def _capture_stop_handler(src):
    """The Finish-recording click handler."""
    start = src.index('$("#recStop").onclick = async () => {')
    # To the handler's own closing brace, not the first `return;` inside it -- the
    # early returns ARE the fix, so stopping at one would hide what is being asserted.
    end = src.index("\n    };", start)
    return src[start:end]


def _go_live_handler(src):
    """The text of the Go live click handler, from its assignment to the end of the
    function it lives in."""
    start = src.index('$("#liveGo").onclick = async () => {')
    end = src.index('const { data } = await uget(sid, "/deploy-bundles");', start)
    return src[start:end]


class AppJsSyntaxTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_the_file_parses(self):
        r = subprocess.run(["node", "--check", APP_JS], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class RenderProfileScopeTest(unittest.TestCase):
    """The exact bug: a binding declared in a narrower block than its readers."""

    def test_mins_is_declared_before_the_stats_block_that_used_to_own_it(self):
        body = _render_profile_body(_source())
        decl = body.index("const mins = runMinutes(")
        stat_row = body.index('const statRow = $("#pStatRow");')
        self.assertLess(decl, stat_row,
                        "`mins` must be declared ABOVE `if (statRow)` — the schedule "
                        "line below reads it, and a block-scoped declaration there is "
                        "a ReferenceError on every render")

    def test_mins_is_declared_exactly_once(self):
        body = _render_profile_body(_source())
        self.assertEqual(len(re.findall(r"const mins\b", body)), 1,
                         "a second declaration means one of the two readers is "
                         "reaching for a binding that is out of its scope")

    def test_every_reader_of_mins_sees_the_hoisted_binding(self):
        """Both readers must sit after the single declaration."""
        body = _render_profile_body(_source())
        decl = body.index("const mins = runMinutes(")
        for m in re.finditer(r"\bmins\b", body):
            self.assertGreaterEqual(m.start(), decl,
                                    "a read of `mins` appears before its declaration")


class RenderSafetyNetTest(unittest.TestCase):
    """What turns the next render bug into a visible error instead of a dead page."""

    def test_unhandled_rejections_are_surfaced(self):
        self.assertIn("unhandledrejection", _source(),
                      "renderProfile is called fire-and-forget everywhere; without "
                      "this listener a throw inside it is completely silent")

    def test_the_render_body_is_guarded(self):
        body = _render_profile_body(_source())
        self.assertIn("console.error(\"renderProfile failed\"", body)
        self.assertIn("Something went wrong showing this page.", body,
                      "a failed render must replace the Loading placeholders")

    def test_the_activity_poll_repaints_at_most_once(self):
        body = _render_profile_body(_source())
        self.assertIn("_activityRepainted", body,
                      "without this guard a server that flips `refreshing` back on "
                      "makes the page re-render every few seconds forever")


class DeployErrorSurvivesTest(unittest.TestCase):
    """A failed deploy used to paint its error into #liveMsg and then call render(),
    which rebuilds that element empty — so the one message naming the cause was
    unreadable, and a deploy that created a live platform agent and then lost it
    looked identical to one that did nothing at all.

    These pin the two halves of the fix: the handler stops before the repaint, and
    the repaint itself redraws the error rather than clearing it.
    """

    def test_the_failed_poll_returns_before_the_repaint(self):
        body = _go_live_handler(_source())
        catch = body.index("} catch (e) {")
        ret = body.index("return;", catch)
        repaint = body.index("await refreshSession(); render();", catch)
        self.assertLess(ret, repaint,
                        "the catch block must return — falling through to render() "
                        "rebuilds #liveBody and erases the error that was just set")

    def test_the_failure_is_held_across_renders(self):
        src = _source()
        self.assertIn("deployError: null", src,
                      "the error has to live on S, not only in the DOM, or any other "
                      "render clears it")
        body = _go_live_handler(src)
        self.assertIn("S.deployError = String(", body)

    def test_the_go_live_body_redraws_a_held_error(self):
        src = _source()
        i = src.index('<p id="liveMsg"')
        para = src[i:src.index("</p>", i)]
        self.assertIn("S.deployError", para,
                      "#liveMsg must be seeded from the held error, so a re-render "
                      "from anywhere redraws the failure instead of blanking it")
        self.assertIn("pre-wrap", para,
                      "these messages carry a JSON payload and a platform agent id")

    def test_a_new_attempt_clears_the_previous_failure(self):
        body = _go_live_handler(_source())
        first = body.index("S.deployError = null;")
        self.assertLess(first, body.index("await upost(sid, \"/deploy\""),
                        "a retry must clear the stale error before it starts")


class CaptureStopSurvivesTest(unittest.TestCase):
    """Finishing a recording used to discard the server's answer.

    Whatever happened — the upload failed, the controller was lost to a restart, the
    recording captured nothing — the handler called loadAndRender(), which finds no new
    session, derives step 0 from the missing raw artifact, and repaints "Record your
    day". One silent bounce for three different failures, each of which the server had
    named in its response.
    """

    def test_a_failed_stop_returns_before_the_repaint(self):
        body = _capture_stop_handler(_source())
        self.assertIn("if (!ok) {", body)
        fail = body.index("if (!ok) {")
        self.assertLess(body.index("return;", fail), body.index("loadAndRender", fail),
                        "a failed stop must not fall through to the repaint that "
                        "erases the reason")

    def test_the_server_s_reason_is_read_rather_than_dropped(self):
        body = _capture_stop_handler(_source())
        self.assertIn("captureStopError(data)", body)
        src = _source()
        for code in ("not_running", "upload_failed"):
            self.assertIn(code, src, f"{code} is a distinct outcome and must be named")

    def test_a_recording_that_captured_nothing_does_not_advance(self):
        """It used to: an empty raw file still creates a session, so the wizard ran the
        whole pipeline over an empty stream and reported finding no workflows — which
        reads as palza failing to understand the work rather than never seeing any."""
        body = _capture_stop_handler(_source())
        self.assertIn("if (!data.raw_event_count) {", body)
        zero = body.index("if (!data.raw_event_count) {")
        self.assertLess(body.index("return;", zero), body.index("loadAndRender", zero))


def _poll_job_source(src):
    start = src.index("async function pollJob(")
    return src[start:src.index("\n}\n", start) + 2]


_POLL_HARNESS = r"""
%(poll)s
let clock = 0;
Date.now = () => clock;
const wait = (ms) => { clock += ms; return Promise.resolve(); };
async function run(states, action, opts) {
  let i = 0, calls = 0;
  clock = 0;
  globalThis.uget = async () => {
    calls++;
    return { data: { state: states[Math.min(i++, states.length - 1)] } };
  };
  try {
    const d = await pollJob("s1", action, null, opts);
    return { result: d.state, calls, clock };
  } catch (e) {
    return { error: e.message, code: e.code || null, calls, clock };
  }
}
(async () => {
  const out = {};
  out.done = await run(["running", "running", "done"], "reconstruct");
  out.lost = await run(["running", "idle", "idle", "idle"], "reconstruct",
                       { idleIsLost: true, everyMs: 3000, maxMs: 7200000 });
  out.idleWaits = await run(["idle"], "discover");
  out.longWait = await run(["running"], "reconstruct", { everyMs: 3000, maxMs: 7200000 });
  out.failed = await run(["running", "error"], "discover");
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class PollJobTest(unittest.TestCase):
    """pollJob run for real in node, with the server and the clock faked. A 28-minute
    recording took ~20 minutes to reconstruct; the screen gave up at 16 and skipped
    discovery, so the tester never saw her workflows."""

    @classmethod
    def setUpClass(cls):
        import json
        js = _POLL_HARNESS % {"poll": _poll_job_source(_source())}
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise AssertionError(r.stderr)
        cls.out = json.loads(r.stdout)

    def test_a_finished_job_returns(self):
        self.assertEqual(self.out["done"]["result"], "done")

    def test_a_job_the_server_lost_stops_quickly_and_says_so(self):
        lost = self.out["lost"]
        self.assertEqual(lost["code"], "lost")
        self.assertLess(lost["clock"], 60 * 1000, "a lost job must not be waited out")

    def test_other_steps_keep_the_sixteen_minute_wait(self):
        idle = self.out["idleWaits"]
        self.assertEqual(idle["code"], "timeout")
        self.assertEqual(idle["clock"], 16 * 60 * 1000)

    def test_the_long_wait_lasts_two_hours(self):
        self.assertEqual(self.out["longWait"]["code"], "timeout")
        self.assertEqual(self.out["longWait"]["clock"], 2 * 60 * 60 * 1000)

    def test_a_server_error_is_still_thrown(self):
        self.assertEqual(self.out["failed"]["error"], "discover failed")


class ReconstructWaitTest(unittest.TestCase):
    def _run_workflows(self):
        src = _source()
        start = src.index("async function runWorkflows(")
        return src[start:src.index("\n}\n", start)]

    def test_the_timeline_step_waits_hours_and_detects_a_lost_job(self):
        body = self._run_workflows()
        call = body[body.index('await pollJob(sid, "reconstruct"'):]
        call = call[:call.index(");") + 2]
        self.assertIn("idleIsLost: true", call)
        self.assertIn("maxMs: 2 * 60 * 60 * 1000", call)

    def test_the_screen_joins_the_servers_discovery_instead_of_starting_another(self):
        """The server starts discovery itself; a second POST after it finished would
        pay for a second run over the same log."""
        body = self._run_workflows()
        check = body.index('uget(sid, "/discover/status")')
        start = body.index('upost(sid, "/discover"')
        self.assertLess(check, start)
        self.assertIn('["running", "done"].includes(disc.state)', body[check:start + 40])

    def test_a_long_wait_is_announced_from_the_estimate(self):
        body = self._run_workflows()
        self.assertIn("d.estimate_seconds > 10 * 60", body)
        self.assertIn("You can close Trace", body)

    def test_a_timeout_is_not_reported_as_a_failure(self):
        body = self._run_workflows()
        self.assertIn('e.code === "timeout"', body)
        self.assertIn('e.code === "lost"', body)


if __name__ == "__main__":
    unittest.main()
