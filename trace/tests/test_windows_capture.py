"""Windows capture — the UI Automation adapter and the platform switches around it.

The COM calls themselves only exist on Windows, so these run the adapter against
fake UIA elements shaped like the real ones (``CurrentControlType``,
``CurrentName``, ``GetCurrentPattern``). What that pins down is the part that
decides what reaches disk: that a Windows control speaks AX's vocabulary so the
shared logic in ``ax.py`` treats it exactly as it treats a Mac one, and that a
password box's value is never even asked for. The live COM path is verified on a
Windows machine (see ``python -m trace.capture.probe``).
"""

import sys
import threading
import types
import unittest
from unittest import mock

from trace.capture import ax, uia, win_window
from trace.capture.keystrokes import KeystrokeCoalescer
from trace.tests.test_ax import Recorder

BUTTON, CHECKBOX, EDIT, DOCUMENT, PANE, WINDOW = 50000, 50002, 50004, 50030, 50033, 50032


class FakePattern:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def QueryInterface(self, _iface):
        return self


class FakeUIAElement:
    """Shaped like an ``IUIAutomationElement``. Records every pattern request so a
    test can prove a password box's value was never asked for."""

    def __init__(self, control_type, name="", value=None, toggle=None,
                 password=False, help_text="", children=None):
        self.CurrentControlType = control_type
        self.CurrentName = name
        self.CurrentHelpText = help_text
        self.CurrentIsPassword = password
        self._value, self._toggle = value, toggle
        self.kids = children or []
        self.pattern_requests = []

    def GetCurrentPattern(self, pattern_id):
        self.pattern_requests.append(pattern_id)
        if pattern_id == uia._PATTERN_VALUE and self._value is not None:
            return FakePattern(CurrentValue=self._value)
        if pattern_id == uia._PATTERN_TOGGLE and self._toggle is not None:
            return FakePattern(CurrentToggleState=self._toggle)
        return None


class FakeWalker:
    def GetFirstChildElement(self, element):
        return element.kids[0] if element.kids else None

    def GetNextSiblingElement(self, element):
        sibs = getattr(element, "_siblings", [])
        i = sibs.index(element)
        return sibs[i + 1] if i + 1 < len(sibs) else None


def link_siblings(root):
    for kid in root.kids:
        kid._siblings = root.kids
        link_siblings(kid)
    return root


def adapter(window=None):
    """A UIAAdapter wired to fakes instead of COM."""
    a = uia.UIAAdapter.__new__(uia.UIAAdapter)
    a._timeout_ms = 1000
    a._ok = True
    a._local = threading.local()
    a._local.tried = True
    a._local.uia = types.SimpleNamespace(ControlViewWalker=FakeWalker())
    a._local.mod = types.SimpleNamespace(
        IUIAutomationValuePattern=None, IUIAutomationTogglePattern=None,
        IUIAutomationRangeValuePattern=None)
    a.focused_window = lambda: window
    return a


class RoleTranslationTest(unittest.TestCase):
    """UIA control types answer in AX's role names, so ax.py needs no second
    vocabulary and both platforms emit the same roles."""

    def test_common_controls_land_on_the_extensions_vocabulary(self):
        for control_type, expected in [(BUTTON, "button"), (EDIT, "textbox"),
                                       (CHECKBOX, "checkbox"), (DOCUMENT, "document"),
                                       (PANE, "group")]:
            self.assertEqual(ax.normalize_role(uia.ax_role(control_type)), expected)

    def test_a_password_box_is_a_password_whatever_its_type(self):
        self.assertEqual(ax.normalize_role(uia.ax_role(EDIT, is_password=True)), "password")

    def test_an_unknown_control_type_is_blank_not_an_error(self):
        self.assertEqual(uia.ax_role(99999), "")

    def test_a_browser_page_is_what_find_web_area_looks_for(self):
        page = FakeUIAElement(DOCUMENT, "Inbox")
        window = link_siblings(FakeUIAElement(WINDOW, "Gmail", children=[
            FakeUIAElement(PANE, children=[page])]))
        self.assertIs(ax.find_web_area(adapter(window), window), page)


class DetailTest(unittest.TestCase):
    def test_a_button_reports_role_and_label(self):
        d = ax.detail_of(adapter(), FakeUIAElement(BUTTON, "Send"))
        self.assertEqual(d, {"role": "button", "label": "Send", "value": ""})

    def test_a_text_field_reports_its_value(self):
        d = ax.detail_of(adapter(), FakeUIAElement(EDIT, "Subject", value="Invoice 42"))
        self.assertEqual(d["value"], "Invoice 42")

    def test_a_checkbox_reports_one_or_zero_like_macos(self):
        on = ax.detail_of(adapter(), FakeUIAElement(CHECKBOX, "Paid", toggle=1))
        off = ax.detail_of(adapter(), FakeUIAElement(CHECKBOX, "Paid", toggle=0))
        self.assertEqual((on["value"], off["value"]), ("1", "0"))

    def test_a_password_value_is_never_even_requested(self):
        box = FakeUIAElement(EDIT, "Password", value="hunter2", password=True)
        d = ax.detail_of(adapter(), box)
        self.assertEqual((d["role"], d["value"]), ("password", ""))
        self.assertEqual(box.pattern_requests, [])

    def test_a_failing_element_degrades_to_nothing(self):
        class Dead:
            def __getattr__(self, _name):
                raise OSError("element no longer available")
        self.assertIsNone(ax.detail_of(adapter(), Dead()))


class UrlTest(unittest.TestCase):
    """UIA keeps the URL in the address bar, not on the page as macOS does."""

    def browser(self, bar_value):
        page = FakeUIAElement(DOCUMENT, "Inbox")
        bar = FakeUIAElement(EDIT, "Address and search bar", value=bar_value)
        window = link_siblings(FakeUIAElement(WINDOW, "Gmail", children=[
            FakeUIAElement(PANE, children=[bar]), FakeUIAElement(PANE, children=[page])]))
        return adapter(window), page

    def test_the_address_bar_supplies_the_url_with_its_scheme_restored(self):
        a, page = self.browser("mail.google.com/mail/u/0/#inbox")
        self.assertEqual(a.url_of(page), "https://mail.google.com/mail/u/0/#inbox")

    def test_a_half_typed_search_is_not_a_url(self):
        a, page = self.browser("quarterly invoices")
        self.assertEqual(a.url_of(page), "")

    def test_chrome_puts_the_url_on_the_page_itself(self):
        # What Chrome actually did on a Windows runner: the Document's value IS the
        # URL, and a file:// address has no dot-name for the address-bar heuristic.
        a, _ = self.browser("")
        page = FakeUIAElement(DOCUMENT, "Trace smoke form",
                              value="file:///C:/Users/runner/form.html")
        self.assertEqual(a.url_of(page), "file:///C:/Users/runner/form.html")

    def test_a_native_window_has_no_url(self):
        a, _ = self.browser("mail.google.com")
        self.assertEqual(a.url_of(FakeUIAElement(PANE, "Toolbar")), "")


class PlatformSwitchTest(unittest.TestCase):
    def test_windows_gets_the_uia_adapter(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertIsInstance(ax.default_adapter(), uia.UIAAdapter)

    def test_macos_keeps_the_ax_adapter(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertIsInstance(ax.default_adapter(), ax.AXAdapter)

    def test_the_uia_adapter_is_unavailable_off_windows(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertFalse(uia.UIAAdapter().available())

    def test_the_windows_window_reader_is_inert_off_windows(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertIsNone(win_window.frontmost())

    def test_windows_permissions_report_granted_not_unknown(self):
        from trace.ui import permissions
        with mock.patch.object(sys, "platform", "win32"):
            for check in (permissions.check_input_monitoring,
                          permissions.check_screen_recording):
                self.assertIs(check()["granted"], True)
            self.assertTrue(permissions.check_all()["ready"])


class AltGrTest(unittest.TestCase):
    """Windows reports AltGr as Ctrl+Alt; it types characters, not shortcuts."""

    def typed(self, keys):
        w = Recorder()
        c = KeystrokeCoalescer(w)
        for k in keys:
            c.on_key(k, "2026-09-28T10:00:00")
        c.flush(ts_end="2026-09-28T10:00:01")
        return w.events[0]["payload"]["text"] if w.events else ""

    def test_an_at_sign_typed_with_altgr_survives(self):
        self.assertEqual(self.typed(["a", "Key.ctrl_l", "Key.alt_gr", "@", "b"]), "a@b")

    def test_ctrl_c_is_still_a_shortcut(self):
        self.assertEqual(self.typed(["a", "Key.ctrl_l", "c"]), "a")


class ClickWorkerTest(unittest.TestCase):
    """Clicks are resolved off the mouse hook, stamped with the time of the click."""

    def test_a_click_is_resolved_on_a_worker_with_the_hooks_timestamp(self):
        hooks = {}

        class FakeListener:
            def __init__(self, on_click):
                hooks["on_click"] = on_click

            def start(self):
                pass

            def stop(self):
                hooks["stopped"] = True

        fake_pynput = types.ModuleType("pynput")
        fake_pynput.mouse = types.SimpleNamespace(Listener=FakeListener)
        from trace.tests.test_ax import FakeAX, el
        class StampRecorder(Recorder):
            def write(self, source, payload, ts=None, **kw):
                rec = super().write(source, payload, ts=ts, **kw)
                rec["ts"] = ts
                return rec
        writer = StampRecorder()
        stamps = iter(["2026-09-28T10:00:01+00:00"])
        with mock.patch.dict(sys.modules, {"pynput": fake_pynput}):
            listener = ax.click_listener(writer, FakeAX(window_el=el("AXButton", "Send")),
                                         clock=lambda: next(stamps))
        hooks["on_click"](10, 20, types.SimpleNamespace(name="left"), True)
        hooks["on_click"](10, 20, types.SimpleNamespace(name="left"), False)  # release
        for _ in range(200):
            if writer.of("click"):
                break
            threading.Event().wait(0.01)
        listener.stop()
        clicks = writer.of("click")
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0]["payload"]["target"]["label"], "Send")
        self.assertEqual(clicks[0]["ts"], "2026-09-28T10:00:01+00:00")
        self.assertTrue(hooks["stopped"])


if __name__ == "__main__":
    unittest.main()
