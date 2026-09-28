"""Clipboard capturer (macOS and Windows, outside-browser).

Closes the copy-paste blind spot for native apps. Polls the macOS pasteboard via
``pbpaste``, or the Windows clipboard; when the contents change, emits a
``clipboard`` raw event.

Windows is read differently on purpose. Only one process can hold the Windows
clipboard open at a time, so opening it twice a second would make a person's own
copies fail at random. ``GetClipboardSequenceNumber`` changes on every copy and is
free to read without opening anything, so the clipboard is opened only when that
number moves — about once per copy.

Limitation (documented): polling the system pasteboard can observe *that the
clipboard changed* (a copy or cut happened somewhere) but cannot by itself tell
copy from cut, nor observe paste. So local pasteboard changes are recorded as
``op: "copy"``. Precise ``copy``/``cut``/``paste`` ops with correct direction come
from the browser extension, which hooks the actual clipboard events in-page. This
local poll exists so a native-app copy is not completely invisible.
"""

import subprocess
import sys
import time

from .raw_writer import now_iso


def read_pasteboard():
    """Return current pasteboard text, or ``None`` if unavailable."""
    if sys.platform == "win32":
        return _read_windows_clipboard()
    try:
        return subprocess.check_output(
            ["pbpaste"], stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        return None


_CF_UNICODETEXT = 13
_win_seq = None       # clipboard sequence number when last read
_win_text = None      # what it held then


def _read_windows_clipboard():
    """Clipboard text on Windows, opening the clipboard only when it has changed
    since the last read (see module docstring)."""
    global _win_seq, _win_text
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        seq = user32.GetClipboardSequenceNumber()
        if seq == _win_seq:
            return _win_text
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
        # Another app may be mid-copy with the clipboard open; give it a moment.
        for _ in range(5):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.02)
        else:
            return _win_text      # still busy: report no change, retry next tick
        try:
            text = ""
            handle = user32.GetClipboardData(_CF_UNICODETEXT)
            if handle:
                ptr = kernel32.GlobalLock(handle)
                if ptr:
                    try:
                        text = ctypes.wstring_at(ptr)
                    finally:
                        kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
        _win_seq, _win_text = seq, text
        return text
    except Exception:
        return None


def clipboard_loop(stop, writer, interval=0.5):
    """Poll the pasteboard and emit a clipboard event on change."""
    last = read_pasteboard()
    while not stop.is_set():
        cur = read_pasteboard()
        if cur is not None and cur != last and cur.strip():
            writer.write("clipboard", {"op": "copy", "content": cur}, ts=now_iso())
            last = cur
        elif cur is not None:
            last = cur
        stop.wait(interval)
