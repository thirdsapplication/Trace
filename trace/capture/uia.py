"""UI Automation adapter (Windows) — the Windows half of the semantic layer.

``ax.py`` holds every capture DECISION — which focus changes to emit, when a text
field flushes, how password fields are caught, how a structured read is bounded —
and reaches the OS only through an adapter. ``AXAdapter`` is the macOS one. This is
the Windows one: the same eleven methods, answered from UI Automation, the tree
Narrator reads. Nothing in ``ax.py`` knows which it is talking to, so a Windows
session emits the same ``target`` / ``click`` / ``structured_read`` events, with
the same role vocabulary, as a Mac one.

The adapter's contract is ``attr(element, <AX attribute name>)``. Rather than teach
``ax.py`` a second vocabulary, UIA answers in AX's: a UIA Button reports
``AXButton``, a browser page (UIA Document) reports ``AXWebArea``, and so on. That
is the whole trick, and ``ax_role`` below is where it lives.

Differences from macOS worth knowing:

1. **No permission.** Windows has no Accessibility or Input Monitoring grant, so
   ``available()`` is simply "is UI Automation reachable". The one hard limit:
   a normal process cannot read an app running as Administrator. Those apps
   record as window titles only, exactly as an ungranted Mac does.

2. **One UIA client per thread.** The focus poller, the window poller (title
   fallback) and the click worker each run on their own thread. COM objects are
   bound to the apartment that made them, so each thread initialises COM itself
   and gets its own ``IUIAutomation``; elements never cross threads.

3. **Timeouts are mandatory**, for the same reason as on macOS: every property
   read is a cross-process call, and a hung app must not hang capture.
   ``IUIAutomation2`` supplies connection and transaction timeouts.

4. **Password values are never read.** UIA flags a password box with
   ``IsPassword``; this adapter returns no value for it at all, before
   ``ax.is_secure`` gets its own look.
"""

import sys
import threading

from . import diag

# UIA control type ids (UIAutomationClient.h) -> the AX role that ax.py already
# understands. Roles AX has no name for are given one that ``normalize_role``
# passes through lowercased, matching the extension's vocabulary.
_CONTROL_TYPES = {
    50000: "AXButton",          # Button
    50001: "calendar",          # Calendar
    50002: "AXCheckBox",        # CheckBox
    50003: "AXComboBox",        # ComboBox
    50004: "AXTextField",       # Edit
    50005: "AXLink",            # Hyperlink
    50006: "AXImage",           # Image
    50007: "AXRow",             # ListItem — a pick from a list, as on macOS
    50008: "AXList",            # List
    50009: "menu",              # Menu
    50010: "AXMenuBar",         # MenuBar
    50011: "AXMenuItem",        # MenuItem
    50012: "progressbar",       # ProgressBar
    50013: "AXRadioButton",     # RadioButton
    50014: "scrollbar",         # ScrollBar
    50015: "AXSlider",          # Slider
    50016: "spinbutton",        # Spinner
    50017: "status",            # StatusBar
    50018: "AXTabGroup",        # Tab
    50019: "tab",               # TabItem
    50020: "AXStaticText",      # Text
    50021: "toolbar",           # ToolBar
    50022: "tooltip",           # ToolTip
    50023: "tree",              # Tree
    50024: "AXRow",             # TreeItem
    50025: "AXGroup",           # Custom
    50026: "AXGroup",           # Group
    50027: "thumb",             # Thumb
    50028: "AXTable",           # DataGrid
    50029: "AXRow",             # DataItem
    50030: "AXWebArea",         # Document — the page in a browser window
    50031: "AXMenuButton",      # SplitButton
    50032: "AXWindow",          # Window
    50033: "AXGroup",           # Pane
    50034: "columnheader",      # Header
    50035: "columnheader",      # HeaderItem
    50036: "AXTable",           # Table
    50037: "titlebar",          # TitleBar
    50038: "separator",         # Separator
    50039: "AXGroup",           # SemanticZoom
    50040: "toolbar",           # AppBar
}
_DOCUMENT = 50030
_EDIT = 50004

# UIA property and pattern ids used below (UIAutomationClient.h).
_PATTERN_VALUE = 10002
_PATTERN_RANGE_VALUE = 10003
_PATTERN_TOGGLE = 10015

# Toggle states -> the 0/1 macOS reports for a checkbox's AXValue.
_TOGGLE_STATE = {0: 0, 1: 1, 2: 0}

# Browser address bars, by the accessible name each browser gives its URL box.
# The page element (Document) carries no URL in UIA, unlike macOS's AXURL.
_ADDRESS_BAR_NAMES = (
    "address and search bar",       # Chrome, Edge (Chromium), Brave
    "search or enter address",      # Firefox
    "search with google or enter address",
)
MAX_CHILDREN = 200                  # a list control can hold thousands of rows
ADDRESS_BAR_MAX_DEPTH = 8
TIMEOUT_MS = 1000


def ax_role(control_type, is_password=False):
    """The AX role name for a UIA control type — the translation that lets
    ``ax.py`` run unchanged. A password box is ``AXSecureTextField``, whatever
    control type it reports."""
    if is_password:
        return "AXSecureTextField"
    return _CONTROL_TYPES.get(control_type, "")


def looks_like_url(text):
    """Whether an address-bar value is a page address. Chromium elides the scheme,
    so ``mail.google.com/mail/u/0`` counts; a half-typed search does not."""
    if not text or " " in text.strip():
        return False
    t = text.strip()
    return t.startswith(("http://", "https://", "file://")) or (
        "." in t.split("/")[0] and not t.startswith("."))


def normalize_url(text):
    """Restore the scheme Chromium's address bar leaves off, so a URL from Windows
    looks like one from the extension or macOS."""
    t = (text or "").strip()
    if not t:
        return ""
    return t if "://" in t else "https://" + t


class UIAAdapter:
    """UI Automation behind the ``AXAdapter`` interface. All COM lives here, so the
    capture logic in ``ax.py`` stays testable with a fake on any platform."""

    def __init__(self, timeout_ms=TIMEOUT_MS):
        self._timeout_ms = timeout_ms
        self._local = threading.local()
        self._ok = sys.platform == "win32" and self._client() is not None

    # --- plumbing ---------------------------------------------------------
    def _client(self):
        """This thread's ``IUIAutomation``, created on first use. None when UI
        Automation cannot be reached (not Windows, comtypes missing)."""
        local = self._local
        if getattr(local, "tried", False):
            return getattr(local, "uia", None)
        local.tried, local.uia, local.mod = True, None, None
        try:
            # Multithreaded apartment: UIA recommends it for clients, and it lets
            # this thread's objects be used without a message pump.
            sys.coinit_flags = 0
            import comtypes
            import comtypes.client
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except OSError:
                pass          # already initialised on this thread — fine
            mod = comtypes.client.GetModule("UIAutomationCore.dll")
            uia = comtypes.client.CreateObject(
                mod.CUIAutomation, interface=mod.IUIAutomation)
            try:
                uia2 = uia.QueryInterface(mod.IUIAutomation2)
                uia2.ConnectionTimeout = self._timeout_ms
                uia2.TransactionTimeout = self._timeout_ms
            except Exception:
                pass          # Windows 7: no timeouts, but still usable
            local.uia, local.mod = uia, mod
        except Exception as e:
            # The one failure that silently removes all click and field capture on
            # Windows (e.g. comtypes bindings missing from a packaged build).
            local.uia = None
            diag.note("uia_init", e)
            diag.fact("uia_unavailable", f"{type(e).__name__}: {e}"[:300])
        return local.uia

    def _mod(self):
        self._client()
        return getattr(self._local, "mod", None)

    def available(self):
        return self._ok

    # --- the adapter interface ---------------------------------------------
    def attr(self, element, name):
        if element is None:
            return None
        try:
            if name == "AXRole":
                return ax_role(element.CurrentControlType,
                               bool(element.CurrentIsPassword))
            if name == "AXSubrole":
                return "AXSecureTextField" if element.CurrentIsPassword else None
            if name == "AXTitle":
                return element.CurrentName or None
            if name == "AXDescription":
                return element.CurrentHelpText or None
            if name == "AXPlaceholderValue":
                return None
            if name == "AXValue":
                return self._value(element)
            if name == "AXURL":
                return None   # see url_of: UIA keeps the URL in the address bar
        except Exception:
            return None
        return None

    def _value(self, element):
        """A control's current value, from whichever pattern it supports. Never
        read for a password box."""
        if element.CurrentIsPassword:
            return None
        mod = self._mod()
        for pattern_id, iface, read in (
            (_PATTERN_VALUE, "IUIAutomationValuePattern",
             lambda p: p.CurrentValue),
            (_PATTERN_TOGGLE, "IUIAutomationTogglePattern",
             lambda p: _TOGGLE_STATE.get(p.CurrentToggleState, 0)),
            (_PATTERN_RANGE_VALUE, "IUIAutomationRangeValuePattern",
             lambda p: p.CurrentValue),
        ):
            try:
                unk = element.GetCurrentPattern(pattern_id)
                if not unk:
                    continue
                value = read(unk.QueryInterface(getattr(mod, iface)))
                if value not in (None, ""):
                    return value
            except Exception:
                continue
        return None

    def children(self, element):
        uia = self._client()
        if element is None or uia is None:
            return []
        out = []
        try:
            walker = uia.ControlViewWalker
            child = walker.GetFirstChildElement(element)
            while child and len(out) < MAX_CHILDREN:
                out.append(child)
                child = walker.GetNextSiblingElement(child)
        except Exception:
            pass
        return out

    def focused_element(self):
        uia = self._client()
        try:
            return uia.GetFocusedElement() if uia else None
        except Exception:
            return None

    def focused_window(self):
        """The foreground top-level window. Taken from the window manager rather
        than by walking up from focus, which fails when nothing has focus."""
        uia = self._client()
        if uia is None:
            return None
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            return uia.ElementFromHandle(hwnd) if hwnd else None
        except Exception:
            return None

    def focused_window_title(self):
        from .ax import _clip
        return _clip(self.attr(self.focused_window(), "AXTitle")) or None

    def content_root(self):
        from .ax import find_web_area
        window = self.focused_window()
        return find_web_area(self, window) or window

    def url_of(self, element):
        """The page URL when ``element`` is a browser page, else "".

        Chromium exposes the URL as the page element's own value — measured on a
        Windows runner, Chrome's Document reported ``file:///C:/...form.html`` — so
        that is read first. The browser's address bar, found by its accessible name,
        is the fallback for browsers that do not.
        """
        if element is None:
            return ""
        try:
            if element.CurrentControlType != _DOCUMENT:
                return ""
        except Exception:
            return ""
        own = self._value(element)
        if isinstance(own, str) and looks_like_url(own):
            return normalize_url(own)
        bar = self._find_address_bar(self.focused_window(), ADDRESS_BAR_MAX_DEPTH)
        value = self._value(bar) if bar is not None else None
        return normalize_url(value) if looks_like_url(value or "") else ""

    def _find_address_bar(self, root, depth):
        if root is None or depth < 0:
            return None
        for child in self.children(root):
            try:
                if (child.CurrentControlType == _EDIT
                        and (child.CurrentName or "").strip().lower() in _ADDRESS_BAR_NAMES):
                    return child
                if child.CurrentControlType == _DOCUMENT:
                    continue      # the page itself: no browser chrome down there
            except Exception:
                continue
            found = self._find_address_bar(child, depth - 1)
            if found is not None:
                return found
        return None

    def element_at(self, x, y):
        uia = self._client()
        mod = self._mod()
        if uia is None or mod is None:
            return None
        try:
            # The typelib's own POINT struct; comtypes rejects the wintypes one.
            return uia.ElementFromPoint(mod.tagPOINT(int(x), int(y)))
        except Exception:
            return None


def make_dpi_aware():
    """Put the process in per-monitor DPI mode, once, before any listener starts.

    Without it, on a scaled display (125%, 150% — the default on most laptops)
    Windows hands a DPI-unaware process scaled mouse coordinates while UI
    Automation expects physical ones, and every click resolves to the wrong
    element. Harmless where it is already set; a no-op off Windows.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703+)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Windows 8.1 fallback
    except Exception:
        pass
