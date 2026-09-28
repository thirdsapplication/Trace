"""Active-window reader (Windows) — the Windows twin of ``window.py``'s Quartz code.

Same contract: ``frontmost()`` returns ``(app, title)`` for the foreground window
and ``frontmost_pid()`` its process id. ``window_loop`` in ``window.py`` drives
both platforms unchanged.

The app name matters more than it looks. macOS reports the name a person sees —
"Google Chrome", "Microsoft Excel" — and discovery matches workflows to connectable
apps by that name. Windows only hands over an executable path (``chrome.exe``), so
the product name is read from the executable's version resource, which is where
Task Manager gets its names. The file name is the fallback.

Plain ctypes against user32/kernel32/version.dll: no extra dependency, and none of
it needs a permission. Window titles are readable on Windows without anything like
macOS's Screen Recording grant.
"""

import ctypes
import os
import sys
from ctypes import wintypes

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_names = {}          # exe path -> display name; a process never renames itself


_declared = False


def _declare():
    """Give the calls that return handles their real types. ctypes defaults every
    return to a C int, which truncates a 64-bit handle."""
    global _declared
    if _declared:
        return
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _declared = True


def _user32():
    _declare()
    return ctypes.windll.user32


def _foreground():
    """``(hwnd, pid)`` of the foreground window, or ``(None, None)``."""
    try:
        hwnd = _user32().GetForegroundWindow()
        if not hwnd:
            return None, None
        pid = wintypes.DWORD()
        _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return hwnd, (pid.value or None)
    except Exception:
        return None, None


def frontmost_pid():
    return _foreground()[1]


def window_title(hwnd):
    try:
        length = _user32().GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32().GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def exe_path(pid):
    try:
        _declare()
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def product_name(path):
    """The FileDescription from an executable's version resource — "Google Chrome"
    for chrome.exe — or "" when it has none."""
    try:
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, data):
            return ""
        # The first language/codepage pair names the block the strings live in.
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation",
                                      ctypes.byref(ptr), ctypes.byref(length)) \
                or length.value < 4:
            return ""
        lang, codepage = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ushort * 2)).contents
        for key in ("FileDescription", "ProductName"):
            sub = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{key}"
            if version.VerQueryValueW(data, sub, ctypes.byref(ptr), ctypes.byref(length)) \
                    and length.value:
                text = ctypes.wstring_at(ptr, length.value).rstrip("\x00").strip()
                if text:
                    return text
    except Exception:
        pass
    return ""


def app_name(path):
    """Display name for an executable, cached per path."""
    if not path:
        return ""
    if path not in _names:
        stem = os.path.splitext(os.path.basename(path))[0]
        _names[path] = product_name(path) or stem
    return _names[path]


def frontmost(title_fallback=None):
    """``(app, title)`` for the foreground window, or None — the same contract as
    ``window.frontmost`` on macOS, including the optional title fallback."""
    if sys.platform != "win32":
        return None
    hwnd, pid = _foreground()
    if not hwnd or not pid:
        return None
    app = app_name(exe_path(pid))
    if not app:
        return None
    title = window_title(hwnd)
    if not title and title_fallback is not None:
        try:
            title = title_fallback() or ""
        except Exception:
            title = ""
    return (app, title)
