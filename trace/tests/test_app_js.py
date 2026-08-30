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


if __name__ == "__main__":
    unittest.main()
