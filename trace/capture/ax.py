"""Accessibility-API capturer (macOS) — the native-app half of the semantic layer.

The Chrome extension tells us what a click MEANT: which control, which label,
which value. Nothing did that outside Chrome, so a session in Safari, Preview, or
a native client recorded window titles and loose keystrokes — enough to see that
work happened, never enough to rebuild it. This module closes that gap using the
same Accessibility tree VoiceOver reads.

It deliberately emits into the EXISTING schema. macOS gives ``AXRole`` /
``AXTitle`` / ``AXValue``; the extension emits ``{role, label, value}``. They are
the same shape, so these are ordinary ``target``, ``click`` and
``structured_read`` events with ``origin="local"`` — no new source type, no schema
change, and the reconstruction prompt already documents all three. Roles are
normalized to the extension's vocabulary (below) so both capturers speak one
language to the model.

Four decisions worth knowing:

1. **Polling, not AXObserver.** AXObserver delivers notifications on a CFRunLoop.
   The capture sidecar is a headless process Tauri spawns with no run loop being
   pumped — ``window.py`` already documents this exact trap costing it every event
   after the first when it used NSWorkspace. So we poll the focused element on the
   same cadence as the window poller and emit on change.

2. **Text fields flush on blur; choice controls emit immediately.** Polling a text
   field mid-typing would emit an event every tick with a growing value, and the
   keystroke stream already carries that text. So a text field emits once, when
   focus leaves, carrying the field's label and final value — the piece keystrokes
   can't supply. A dropdown or checkbox has no keystrokes at all, so it emits the
   moment its value changes. This mirrors how ``KeystrokeCoalescer`` flushes on
   window change.

3. **A messaging timeout is mandatory.** Every attribute read is synchronous IPC
   into the target app. Without ``AXUIElementSetMessagingTimeout`` a wedged app
   blocks the capture thread indefinitely.

4. **Secure fields are never read.** ``AXSecureTextField`` values are dropped
   before they are built into a payload, closing the native-password gap the
   README lists as a v1 limitation.

Requires macOS Accessibility permission (``AXIsProcessTrusted``), which
``trace.ui.permissions`` already checks. Everything degrades to "emit nothing"
rather than raising, so an ungranted or non-macOS environment simply captures
what it captured before.
"""

from .raw_writer import now_iso

# --- vocabulary -----------------------------------------------------------

# AX role -> the role vocabulary the browser extension already emits, so the model
# sees one set of names whichever capturer produced the event.
ROLE_MAP = {
    "AXButton": "button",
    "AXTextField": "textbox",
    "AXTextArea": "textbox",
    "AXSecureTextField": "password",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio",
    "AXPopUpButton": "combobox",
    "AXComboBox": "combobox",
    "AXMenuItem": "menuitem",
    "AXMenuButton": "button",
    "AXLink": "link",
    "AXStaticText": "text",
    "AXHeading": "heading",
    "AXSlider": "slider",
    "AXTable": "table",
    "AXRow": "row",
    "AXCell": "cell",
    "AXList": "list",
    "AXTabGroup": "tablist",
    "AXImage": "image",
    "AXWebArea": "document",
}

# Roles whose value the worker sets by choosing, not typing — no keystroke event
# will carry it, so the value change is emitted the moment it is seen.
CHOICE_ROLES = {"combobox", "checkbox", "radio", "menuitem", "slider", "row"}
# Roles whose value the keystroke stream already carries; emitted once on blur.
TEXT_ROLES = {"textbox"}
# Never read, at any point.
SECURE_ROLES = {"password"}

MAX_VALUE_CHARS = 500      # a text area can hold a novel; the label is the signal
MAX_WALK_ELEMENTS = 60     # a structured read is a sample, not a DOM dump
MAX_WALK_DEPTH = 12
# The page in a browser window sits several levels below the window root (6 in
# Safari, measured). Bounded so the search cannot wander a deep native tree.
WEB_AREA_MAX_DEPTH = 10


def normalize_role(ax_role):
    """Map an AX role onto the extension's vocabulary. Unknown roles degrade to a
    lowercased, de-prefixed form rather than being dropped — a role we have not
    seen is still more informative than nothing."""
    if not ax_role:
        return ""
    if ax_role in ROLE_MAP:
        return ROLE_MAP[ax_role]
    return ax_role[2:].lower() if ax_role.startswith("AX") else ax_role.lower()


def _clip(value):
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\n", " ").strip()
    return text[:MAX_VALUE_CHARS]


# --- pure logic (adapter-injected, so it is testable without a Mac) --------

def detail_of(ax, element):
    """Build the ``{role, label, value}`` detail for one element, or None if the
    element yields nothing worth recording.

    Secure fields keep their role and label — knowing a password field was focused
    is useful context — but never their value.
    """
    if element is None:
        return None
    role = normalize_role(ax.attr(element, "AXRole"))
    label = _clip(ax.attr(element, "AXTitle")
                  or ax.attr(element, "AXDescription")
                  or ax.attr(element, "AXPlaceholderValue"))
    value = "" if role in SECURE_ROLES else _clip(ax.attr(element, "AXValue"))
    if not role and not label and not value:
        return None
    return {"role": role, "label": label, "value": value}


def walk(ax, root, max_elements=MAX_WALK_ELEMENTS, max_depth=MAX_WALK_DEPTH):
    """Breadth-first sample of an element subtree, capped in both directions.

    Breadth-first on purpose: the controls a worker acts on sit near the top of a
    window's tree, while depth tends to run into layout containers. Both caps are
    load-bearing — every step here is IPC into another process, and an uncapped
    walk of a large admin page is measured in seconds, not milliseconds.
    """
    if root is None:
        return []
    out, seen, frontier, depth = [], set(), [root], 0
    while frontier and depth < max_depth and len(out) < max_elements:
        nxt = []
        for el in frontier:
            for child in ax.children(el) or []:
                d = detail_of(ax, child)
                if d and (d["label"] or d["value"]):
                    sig = (d["role"], d["label"], d["value"])
                    if sig not in seen:
                        seen.add(sig)
                        out.append(d)
                        if len(out) >= max_elements:
                            return out
                nxt.append(child)
        frontier, depth = nxt, depth + 1
    return out


def find_web_area(ax, root, max_depth=WEB_AREA_MAX_DEPTH):
    """The ``AXWebArea`` under ``root`` — the page inside a browser window — or None.

    Anchoring a structured read on the WINDOW is wrong in a browser, and wrong in a
    way that looks like it is working. Measured in Safari on 2026-08-19: the web
    area sits 6 levels down, and a breadth-first walk from the window spent 54 of
    its 60-element budget on the tab bar, History, AutoFill, Share and the titles
    of every open tab — reaching ZERO page content. Those tab titles are also other
    people's business ending up in a capture, which is its own reason not to.

    Depth-first, because the page is deep and narrow rather than wide, and bounded
    because every step is IPC into the browser.
    """
    if root is None or max_depth < 0:
        return None
    if ax.attr(root, "AXRole") == "AXWebArea":
        return root
    for child in ax.children(root) or []:
        found = find_web_area(ax, child, max_depth - 1)
        if found is not None:
            return found
    return None


def focus_signature(detail):
    """Identity of the focused element for change detection. Role+label, NOT value
    — otherwise every keystroke would read as a new element and the blur-flush
    would never fire."""
    return None if detail is None else (detail["role"], detail["label"])


# --- the pyobjc adapter ---------------------------------------------------

class AXAdapter:
    """Thin wrapper over the Accessibility API. All pyobjc lives here so the logic
    above stays testable with a fake.

    Everything hangs off a per-application element built from the frontmost PID,
    NOT off ``AXUIElementCreateSystemWide()``. That is not a stylistic preference:
    verified on macOS 15 inside this process, every system-wide read returns
    kAXErrorCannotComplete (-25204), while the identical read against
    ``AXUIElementCreateApplication(frontmost_pid)`` succeeds. It is the same trap
    ``window.py`` documents for NSWorkspace — both want a run loop the headless
    capture sidecar never pumps — and it fails the same silent way: no exception,
    no events, a session that looks like it recorded nothing happening.
    """

    def __init__(self, timeout=1.0):
        self._timeout = timeout
        self._ok = False
        self._pid = None
        self._app = None
        try:
            from ApplicationServices import AXIsProcessTrusted
            self._ok = bool(AXIsProcessTrusted())
        except Exception:
            self._ok = False

    def available(self):
        return self._ok

    def _app_element(self):
        """AX element for the frontmost app, rebuilt whenever the frontmost app
        changes and cached in between — creating one per poll would be an extra
        IPC handshake several times a second."""
        from .window import frontmost_pid
        pid = frontmost_pid()
        if pid is None:
            return None
        if pid != self._pid or self._app is None:
            try:
                from ApplicationServices import (
                    AXUIElementCreateApplication, AXUIElementSetMessagingTimeout,
                )
                app = AXUIElementCreateApplication(pid)
                # Non-negotiable: without this a hung app blocks the capture thread.
                AXUIElementSetMessagingTimeout(app, self._timeout)
                self._pid, self._app = pid, app
            except Exception:
                self._pid, self._app = None, None
        return self._app

    def _copy(self, element, attribute):
        if element is None:
            return None
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue
            err, value = AXUIElementCopyAttributeValue(element, attribute, None)
            return value if err == 0 else None
        except Exception:
            return None

    def attr(self, element, name):
        return self._copy(element, name)

    def children(self, element):
        kids = self._copy(element, "AXChildren")
        try:
            return list(kids) if kids else []
        except TypeError:
            return []

    def focused_element(self):
        return self._copy(self._app_element(), "AXFocusedUIElement")

    def focused_window(self):
        return self._copy(self._app_element(), "AXFocusedWindow")

    def focused_window_title(self):
        """Window title WITHOUT Screen Recording.

        ``kCGWindowName`` — the path ``window.py`` uses — is only populated for a
        process holding Screen Recording, and its absence silently collapses a
        whole session to one blank event. AX reads the same title under the
        Accessibility grant instead, so this doubles as the fallback that makes
        Screen Recording optional.
        """
        return _clip(self._copy(self.focused_window(), "AXTitle")) or None

    def content_root(self):
        """Where a structured read should start: the PAGE in a browser window, the
        window itself in a native app. See ``find_web_area`` for why the window is
        the wrong anchor in a browser."""
        window = self.focused_window()
        return find_web_area(self, window) or window

    def url_of(self, element):
        """The page URL for an already-resolved content root, or "" for a native
        window.

        Takes the element rather than finding it again: ``find_web_area`` is a
        depth-first descent through the browser's whole chrome subtree, and doing
        that twice per structured read would double the IPC for no new information.

        Only the browser extension could supply a URL before, so a local capture in
        Safari had none and an SPA route change was at best a changed window title.
        ``AXURL`` gives the real address under the Accessibility grant alone —
        verified returning a full admin.shopify.com URL.
        """
        url = self._copy(element, "AXURL")
        return _clip(str(url)) if url else ""

    def element_at(self, x, y):
        """The element under the cursor. Resolved against the frontmost app rather
        than system-wide, for the reason in the class docstring — and the frontmost
        app is the one being clicked, so this is also the correct scope."""
        app = self._app_element()
        if app is None:
            return None
        try:
            from ApplicationServices import AXUIElementCopyElementAtPosition
            err, el = AXUIElementCopyElementAtPosition(app, float(x), float(y), None)
            return el if err == 0 else None
        except Exception:
            return None


# --- capture loops --------------------------------------------------------

def ax_loop(stop, writer, ax=None, interval=0.4, read_interval=20.0, clock=now_iso):
    """Poll the focused element and emit ``target`` (and periodic
    ``structured_read``) events until ``stop`` is set.

    Emits nothing at all when Accessibility is unavailable, so callers can start
    this unconditionally.
    """
    ax = ax or AXAdapter()
    if not ax.available():
        return

    focus_sig = None        # identity of the element currently focused
    current = None          # its most recently seen detail
    entered_with = None     # its value when focus arrived
    window_title = ""
    last_read_at = None
    read_window = None

    def emit(detail, changed):
        payload = dict(detail, window=window_title)
        if changed:
            payload["changed"] = True
        writer.write("target", payload, ts=clock())

    def flush_text_field():
        """A text field reports once, on blur, carrying its label and final value.
        The keystrokes are already in the stream; what they cannot say is which
        field received them."""
        if (current and current["role"] in TEXT_ROLES
                and current["value"] and current["value"] != entered_with):
            emit(current, changed=True)

    while not stop.is_set():
        try:
            element = ax.focused_element()
            detail = detail_of(ax, element)
            sig = focus_signature(detail)

            if sig != focus_sig:
                flush_text_field()
                window_title = ax.focused_window_title() or ""
                if detail is not None:
                    emit(detail, changed=False)
                focus_sig, current, entered_with = sig, detail, (detail or {}).get("value")
            elif detail is not None and current is not None:
                if detail["value"] != current["value"]:
                    if detail["role"] in CHOICE_ROLES:
                        emit(detail, changed=True)
                        entered_with = detail["value"]
                    current = detail

            # A periodic structured read gives the model the page around the
            # action. Throttled hard, and re-read on a window change, because a
            # tree walk is the most expensive thing this module does.
            now = clock()
            if window_title and (read_window != window_title
                                 or last_read_at is None
                                 or _seconds_between(last_read_at, now) >= read_interval):
                root = ax.content_root()
                elements = walk(ax, root)
                if elements:
                    writer.write("structured_read",
                                 {"anchor": "focus", "url": ax.url_of(root),
                                  "window": window_title, "elements": elements},
                                 ts=now)
                last_read_at, read_window = now, window_title
        except Exception:
            # A capture loop must never take the session down with it; a transient
            # AX failure (app quit mid-read) is expected, not exceptional.
            pass
        stop.wait(interval)

    flush_text_field()


def _seconds_between(a, b):
    """Seconds between two ISO timestamps, or a large number if unparseable — an
    unreadable clock should let the next read happen, not block it forever."""
    import datetime
    try:
        return (datetime.datetime.fromisoformat(b)
                - datetime.datetime.fromisoformat(a)).total_seconds()
    except Exception:
        return float("inf")


def click_listener(writer, ax=None, clock=now_iso):
    """A pynput mouse listener that resolves each click to the element under the
    cursor and writes a ``click`` event with a populated ``target``.

    This is what makes a button-pushing workflow legible. pynput is already a
    dependency and already needs Input Monitoring, so this adds no new permission
    beyond the Accessibility grant AX itself requires. Returns the started
    listener, or None when unavailable.
    """
    ax = ax or AXAdapter()
    if not ax.available():
        return None
    try:
        from pynput import mouse
    except Exception:
        return None

    def on_click(x, y, button, pressed):
        if not pressed:
            return
        try:
            detail = detail_of(ax, ax.element_at(x, y))
            writer.write("click",
                         {"x": x, "y": y, "button": _button_name(button),
                          "target": detail},
                         ts=clock())
        except Exception:
            pass

    listener = mouse.Listener(on_click=on_click)
    listener.start()
    return listener


def _button_name(button):
    """pynput Button -> the schema's enum, which only admits left/right/middle."""
    name = getattr(button, "name", str(button)).replace("Button.", "")
    return name if name in ("left", "right", "middle") else "left"
