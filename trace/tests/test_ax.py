"""Accessibility capture — the native-app half of the semantic layer.

Everything here runs against a fake AX adapter, deliberately: the real one is
pyobjc talking to another process across an IPC boundary, which no CI can
exercise. What CAN be pinned down without a Mac is the logic that decides WHAT to
emit, and that is where the failure modes live — a text field emitting on every
poll tick, a password value reaching disk, an uncapped tree walk. The pyobjc
adapter itself is verified by hand (see the manual test script).
"""

import json
import os
import tempfile
import unittest

from trace.capture import ax, raw_writer, sources, window
from trace.capture.raw_writer import RawWriter


class FakeElement:
    def __init__(self, attrs=None, children=None):
        self.attrs = attrs or {}
        self.kids = children or []


def el(role, label="", value="", children=None):
    return FakeElement({"AXRole": role, "AXTitle": label, "AXValue": value}, children)


class FakeAX:
    """Adapter stand-in. ``script`` is the sequence of focused elements returned on
    successive polls; the last entry repeats."""

    def __init__(self, script=None, title="Untitled", window_el=None, ok=True):
        self.script = list(script or [])
        self.title = title
        self.window_el = window_el
        self._ok = ok
        self.walks = 0

    def available(self):
        return self._ok

    def attr(self, element, name):
        return element.attrs.get(name) if element is not None else None

    def children(self, element):
        return element.kids if element is not None else []

    def focused_element(self):
        if not self.script:
            return None
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    def focused_window(self):
        self.walks += 1
        return self.window_el

    def focused_window_title(self):
        return self.title

    def content_root(self):
        # Same rule the real adapter uses, so tests exercise find_web_area.
        return ax.find_web_area(self, self.window_el) or self.window_el

    def url_of(self, element):
        return "https://admin.shopify.com/products/new" if element is not None else ""

    def element_at(self, x, y):
        return self.window_el


class ExplodingAX(FakeAX):
    def focused_element(self):
        raise RuntimeError("app went away mid-read")


class NTicks:
    """A stop-event that lets the loop run exactly ``n`` iterations."""

    def __init__(self, n):
        self.left = n

    def is_set(self):
        self.left -= 1
        return self.left < 0

    def wait(self, _interval):
        pass


class Recorder:
    """Writer stand-in that keeps the events instead of writing them."""

    def __init__(self):
        self.events = []
        self.paused = False

    def write(self, source, payload, ts=None, origin=raw_writer.LOCAL):
        self.events.append({"source": source, "payload": payload, "origin": origin})
        return self.events[-1]

    def of(self, source):
        return [e for e in self.events if e["source"] == source]


def clock_from(start=0):
    box = {"n": start}

    def clock():
        box["n"] += 1
        return f"2026-08-19T00:00:{box['n']:02d}+00:00"
    return clock


# --- vocabulary + detail --------------------------------------------------

class RoleTest(unittest.TestCase):
    def test_known_roles_map_to_the_extensions_vocabulary(self):
        """Both capturers must speak one language to the model, or 'button' and
        'AXButton' read as two different things."""
        self.assertEqual(ax.normalize_role("AXButton"), "button")
        self.assertEqual(ax.normalize_role("AXPopUpButton"), "combobox")
        self.assertEqual(ax.normalize_role("AXTextArea"), "textbox")

    def test_unknown_roles_degrade_rather_than_vanish(self):
        self.assertEqual(ax.normalize_role("AXSomethingNew"), "somethingnew")
        self.assertEqual(ax.normalize_role(""), "")


class DetailTest(unittest.TestCase):
    def test_password_value_is_never_built_into_a_payload(self):
        """The native-password gap the README lists. Role and label survive —
        knowing a password field was focused is useful; the value never is."""
        d = ax.detail_of(FakeAX(), el("AXSecureTextField", "Password", "hunter2"))
        self.assertEqual(d["role"], "password")
        self.assertEqual(d["label"], "Password")
        self.assertEqual(d["value"], "")

    def test_label_falls_back_through_description_then_placeholder(self):
        a = FakeAX()
        described = FakeElement({"AXRole": "AXButton", "AXDescription": "Save product"})
        placeheld = FakeElement({"AXRole": "AXTextField", "AXPlaceholderValue": "SKU"})
        self.assertEqual(ax.detail_of(a, described)["label"], "Save product")
        self.assertEqual(ax.detail_of(a, placeheld)["label"], "SKU")

    def test_long_values_are_clipped_and_newlines_flattened(self):
        d = ax.detail_of(FakeAX(), el("AXTextArea", "Description", "x" * 5000))
        self.assertEqual(len(d["value"]), ax.MAX_VALUE_CHARS)
        d2 = ax.detail_of(FakeAX(), el("AXTextField", "Note", "a\nb"))
        self.assertEqual(d2["value"], "a b")

    def test_empty_element_yields_nothing(self):
        self.assertIsNone(ax.detail_of(FakeAX(), FakeElement()))
        self.assertIsNone(ax.detail_of(FakeAX(), None))

    def test_focus_signature_ignores_value(self):
        """If the signature included the value, every keystroke would look like a
        new element and the blur-flush would never fire."""
        a, b = el("AXTextField", "Price", "24"), el("AXTextField", "Price", "24.99")
        fake = FakeAX()
        self.assertEqual(ax.focus_signature(ax.detail_of(fake, a)),
                         ax.focus_signature(ax.detail_of(fake, b)))


class WalkTest(unittest.TestCase):
    def test_element_cap_holds(self):
        """Uncapped, this is IPC per node over a large admin page — seconds, not
        milliseconds."""
        root = FakeElement(children=[el("AXTextField", f"F{i}", str(i))
                                     for i in range(500)])
        self.assertEqual(len(ax.walk(FakeAX(), root, max_elements=10)), 10)

    def test_depth_cap_holds(self):
        deep = el("AXStaticText", "bottom", "v")
        for i in range(30):
            deep = FakeElement({"AXRole": "AXGroup"}, [deep])
        self.assertEqual(ax.walk(FakeAX(), deep, max_depth=3), [])

    def test_identical_elements_are_reported_once(self):
        root = FakeElement(children=[el("AXStaticText", "Total", "$10")] * 5)
        self.assertEqual(len(ax.walk(FakeAX(), root)), 1)

    def test_unlabelled_containers_are_skipped_but_traversed(self):
        """Layout wrappers carry nothing, yet the controls beneath them do."""
        inner = el("AXButton", "Save", "")
        root = FakeElement(children=[FakeElement({"AXRole": "AXGroup"}, [inner])])
        self.assertEqual([d["label"] for d in ax.walk(FakeAX(), root)], ["Save"])

    def test_no_root_is_not_an_error(self):
        self.assertEqual(ax.walk(FakeAX(), None), [])


# --- the loop -------------------------------------------------------------

class LoopTest(unittest.TestCase):
    def _run(self, script, ticks, **kw):
        rec = Recorder()
        fake = FakeAX(script, title=kw.pop("title", "Add product · Shopify"))
        ax.ax_loop(NTicks(ticks), rec, fake, read_interval=10 ** 9,
                   clock=clock_from(), **kw)
        return rec

    def test_focus_change_emits_a_context_target(self):
        rec = self._run([el("AXButton", "Save", "")], 1)
        [ev] = rec.of("target")
        self.assertEqual(ev["payload"]["label"], "Save")
        self.assertEqual(ev["payload"]["window"], "Add product · Shopify")
        self.assertNotIn("changed", ev["payload"])   # context, not authored content

    def test_a_dropdown_choice_is_reported_the_moment_it_changes(self):
        """No keystrokes accompany a dropdown, so this event is the ONLY record of
        the decision — the Shopify status/vendor case."""
        rec = self._run([el("AXPopUpButton", "Status", "Draft"),
                         el("AXPopUpButton", "Status", "Active")], 2)
        targets = rec.of("target")
        self.assertEqual(len(targets), 2)
        self.assertNotIn("changed", targets[0]["payload"])
        self.assertTrue(targets[1]["payload"]["changed"])
        self.assertEqual(targets[1]["payload"]["value"], "Active")

    def test_a_text_field_reports_once_on_blur_not_once_per_tick(self):
        """Polling a field mid-typing would emit an event per tick with a growing
        value, and the keystroke stream already carries the text. What it cannot
        carry is WHICH field received it — hence one event, on blur, with the
        label and the final value."""
        rec = self._run([el("AXTextField", "Price", ""),
                         el("AXTextField", "Price", "24"),
                         el("AXTextField", "Price", "24.9"),
                         el("AXTextField", "Price", "24.99"),
                         el("AXButton", "Save", "")], 5)
        payloads = [e["payload"] for e in rec.of("target")]
        changed = [p for p in payloads if p.get("changed")]
        self.assertEqual(len(changed), 1, "one flush, not one per poll")
        self.assertEqual(changed[0]["label"], "Price")
        self.assertEqual(changed[0]["value"], "24.99")
        self.assertEqual([p["label"] for p in payloads], ["Price", "Price", "Save"])

    def test_an_untouched_text_field_flushes_nothing(self):
        rec = self._run([el("AXTextField", "Price", "24.99"),
                         el("AXButton", "Save", "")], 2)
        self.assertEqual([p["payload"].get("changed") for p in rec.of("target")],
                         [None, None])

    def test_the_last_field_is_flushed_when_capture_stops(self):
        """Otherwise the final field of every session — often the one that matters
        — is silently dropped."""
        rec = self._run([el("AXTextField", "SKU", ""),
                         el("AXTextField", "SKU", "ABC-1")], 2)
        changed = [e["payload"] for e in rec.of("target") if e["payload"].get("changed")]
        self.assertEqual([c["value"] for c in changed], ["ABC-1"])

    def test_a_structured_read_accompanies_a_new_window(self):
        page = FakeElement(children=[el("AXStaticText", "Inventory", "12"),
                                     el("AXButton", "Save", "")])
        rec = Recorder()
        ax.ax_loop(NTicks(1), rec, FakeAX([el("AXButton", "Save")], window_el=page),
                   clock=clock_from())
        [read] = rec.of("structured_read")
        self.assertEqual(read["payload"]["anchor"], "focus")
        self.assertEqual(sorted(d["label"] for d in read["payload"]["elements"]),
                         ["Inventory", "Save"])

    def test_no_permission_means_no_events_and_no_crash(self):
        rec = Recorder()
        ax.ax_loop(NTicks(5), rec, FakeAX([el("AXButton", "Save")], ok=False))
        self.assertEqual(rec.events, [])

    def test_a_failing_adapter_never_takes_the_session_down(self):
        """An app quitting mid-read is expected, not exceptional. If this raised,
        one closed window would end the recording."""
        rec = Recorder()
        ax.ax_loop(NTicks(3), rec, ExplodingAX([el("AXButton", "x")]))
        self.assertEqual(rec.events, [])


class ClickTest(unittest.TestCase):
    def test_button_names_are_coerced_to_the_schema_enum(self):
        """raw_event.schema.json admits only left/right/middle; pynput has more."""
        class B:
            def __init__(self, name):
                self.name = name
        self.assertEqual(ax._button_name(B("left")), "left")
        self.assertEqual(ax._button_name(B("middle")), "middle")
        self.assertEqual(ax._button_name(B("button8")), "left")

    def test_no_listener_without_permission(self):
        self.assertIsNone(ax.click_listener(Recorder(), FakeAX(ok=False)))


# --- origin stamping ------------------------------------------------------

class OriginTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "raw_events.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def _rows(self):
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_local_is_the_default_and_browser_is_explicit(self):
        w = RawWriter(self.path, reset=True)
        w.write("window", {"app": "Safari", "title": "Shopify"})
        w.write("click", {"x": 1, "y": 2, "button": "left"}, origin=raw_writer.BROWSER)
        self.assertEqual([r["payload"]["origin"] for r in self._rows()],
                         ["local", "browser"])

    def test_an_unknown_origin_is_refused(self):
        w = RawWriter(self.path, reset=True)
        with self.assertRaises(ValueError):
            w.write("window", {"app": "a", "title": "b"}, origin="guess")

    def test_redaction_still_applies_and_origin_survives_it(self):
        w = RawWriter(self.path, reset=True)
        w.write("target", {"role": "textbox", "label": "Key",
                           "value": "sk-abcdefghijklmnopqrstuvwxyz"})
        payload = self._rows()[0]["payload"]
        self.assertEqual(payload["value"], "[REDACTED]")
        self.assertEqual(payload["origin"], "local")

    def test_old_sessions_without_origin_are_unknown_not_assumed(self):
        """An unstamped stream must not read as proof that a capturer was live."""
        rows = [{"ts": "t", "source": "click", "payload": {}}]
        self.assertEqual(sources.summarize_origins(rows), {"unknown": 1})


# --- the window-title fallback -------------------------------------------

class TitleFallbackTest(unittest.TestCase):
    def test_fallback_supplies_a_title_quartz_could_not(self):
        """This is what demotes Screen Recording from load-bearing to optional."""
        self.assertEqual(window._fallback_title(lambda: "Add product · Shopify"),
                         "Add product · Shopify")

    def test_a_broken_fallback_degrades_to_todays_blank_title(self):
        def boom():
            raise RuntimeError("AX unavailable")
        self.assertEqual(window._fallback_title(boom), "")
        self.assertEqual(window._fallback_title(None), "")


def safari_like_window():
    """A browser window shaped like the real thing: the page buried under chrome.

    Modelled on Safari measured on 2026-08-19 — tab bar, toolbar buttons and one
    radio per open tab sitting above an AXWebArea six levels down.
    """
    page = FakeElement({"AXRole": "AXWebArea"}, [
        el("AXTextField", "Title", "Blue Hoodie"),
        el("AXPopUpButton", "Status", "Active"),
        el("AXButton", "Save", ""),
    ])
    deep = page
    for _ in range(5):                       # bury it the way a real browser does
        deep = FakeElement({"AXRole": "AXGroup"}, [deep])
    chrome = [el("AXButton", "History"), el("AXButton", "AutoFill"),
              el("AXButton", "Share"), el("AXButton", "New Tab")]
    chrome += [el("AXRadioButton", f"Untitled query | SQL Editor | Supabase {i}", "False")
               for i in range(70)]           # every open tab's title
    return FakeElement({"AXRole": "AXWindow"}, chrome + [deep])


class WebAreaTest(unittest.TestCase):
    """The browser anchoring bug: a structured read taken from the WINDOW captures
    the browser, not the page — and quietly records every open tab's title while
    it does."""

    def test_the_page_is_found_beneath_the_chrome(self):
        web = ax.find_web_area(FakeAX(), safari_like_window())
        self.assertIsNotNone(web)
        labels = [d["label"] for d in ax.walk(FakeAX(), web)]
        self.assertEqual(sorted(labels), ["Save", "Status", "Title"])

    def test_a_native_window_has_no_web_area(self):
        native = FakeElement({"AXRole": "AXWindow"}, [el("AXButton", "Send")])
        self.assertIsNone(ax.find_web_area(FakeAX(), native))

    def test_the_search_is_depth_bounded(self):
        deep = FakeElement({"AXRole": "AXWebArea"})
        for _ in range(30):
            deep = FakeElement({"AXRole": "AXGroup"}, [deep])
        self.assertIsNone(ax.find_web_area(FakeAX(), deep, max_depth=4))

    def test_walking_from_the_window_is_what_went_wrong(self):
        """The control. Anchored on the window, the element budget is spent on tab
        titles and the form is never reached — which is what shipped."""
        labels = [d["label"] for d in ax.walk(FakeAX(), safari_like_window())]
        self.assertNotIn("Title", labels)
        self.assertNotIn("Status", labels)
        self.assertTrue(any("SQL Editor" in l for l in labels))

    def test_a_structured_read_captures_the_page_and_its_url(self):
        rec = Recorder()
        fake = FakeAX([el("AXTextField", "Title", "Blue Hoodie")],
                      window_el=safari_like_window())
        ax.ax_loop(NTicks(1), rec, fake, clock=clock_from())
        [read] = rec.of("structured_read")
        labels = [e["label"] for e in read["payload"]["elements"]]
        self.assertEqual(sorted(labels), ["Save", "Status", "Title"])
        self.assertFalse([l for l in labels if "SQL Editor" in l],
                         "another tab's title leaked into the capture")
        self.assertTrue(read["payload"]["url"], "AXURL should give the real page URL")


def _ax_granted():
    try:
        return ax.AXAdapter().available()
    except Exception:
        return False


@unittest.skipUnless(_ax_granted(), "Accessibility not granted on this machine")
class RealAdapterTest(unittest.TestCase):
    """The one class of bug a fake adapter cannot catch: the pyobjc calls running
    but returning nothing.

    The first version of ``AXAdapter`` hung everything off
    ``AXUIElementCreateSystemWide()``. Every unit test above passed, the module
    imported, no exception was raised anywhere — and in this process every
    system-wide read returns kAXErrorCannotComplete, so it would have captured
    exactly zero events forever. These assertions are cheap and they are the only
    thing standing between that regression and a silent reappearance.
    """

    def setUp(self):
        self.ax = ax.AXAdapter()

    def test_a_real_focused_window_resolves(self):
        self.assertIsNotNone(
            self.ax.focused_window(),
            "no focused window — AX reads are returning nothing, which is how the "
            "system-wide-element regression presents")

    def test_a_real_window_has_a_title_without_screen_recording(self):
        """The claim that makes Screen Recording optional. If this is None, that
        claim is false and the permission is still load-bearing."""
        self.assertTrue(self.ax.focused_window_title())

    def test_a_real_tree_walk_returns_labelled_elements(self):
        elements = ax.walk(self.ax, self.ax.focused_window())
        self.assertTrue(elements, "focused window exposed no readable elements")
        self.assertTrue(any(e["label"] or e["value"] for e in elements))
        self.assertLessEqual(len(elements), ax.MAX_WALK_ELEMENTS)


if __name__ == "__main__":
    unittest.main()
