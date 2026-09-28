# PyInstaller spec for the Trace desktop capture node (macOS and Windows sidecar).
#
# Build (run from the repo root):
#     pyinstaller desktop/trace-node.spec
# Output: dist/trace-node/  (a directory bundle; no decompression on startup,
# so the app launches in seconds instead of 30-60s for onefile mode).
#
# This freezes the existing `trace.ui.app` server. In desktop mode it runs
# capture locally and proxies auth + pipeline to the cloud, so the heavy
# cloud-only deps (anthropic, psycopg) are EXCLUDED — they're imported lazily and
# only on code paths that, in the app, are proxied to the cloud. This keeps the
# bundle small. pynput + pyobjc (macOS) or pynput + comtypes (Windows) are the
# capture deps and MUST be bundled.

import os
import sys

# The spec runs with its own dir as CWD-ish; point pathex at the repo root so the
# `trace` package and its data dirs resolve from a clean clone.
ROOT = os.path.abspath(os.path.join(os.path.dirname(SPECPATH), "."))

block_cipher = None

# PyInstaller frequently misses pynput's platform backend and the per-OS modules
# capture imports lazily (window titles, element reads, permission checks).
if sys.platform == "win32":
    # comtypes writes the UI Automation bindings (comtypes.gen.UIAutomationClient)
    # the first time they are asked for. An installed app cannot write inside its
    # own bundle, so generate them HERE, at build time, and bundle the result —
    # otherwise the shipped app would have no click or field capture and no error.
    import comtypes.client
    from PyInstaller.utils.hooks import collect_submodules
    comtypes.client.GetModule("UIAutomationCore.dll")
    CAPTURE_IMPORTS = [
        "pynput.keyboard._win32", "pynput.mouse._win32", "pynput._util.win32",
        "comtypes", "comtypes.client", "comtypes.stream",
    ] + collect_submodules("comtypes.gen")
else:
    CAPTURE_IMPORTS = [
        "pynput.keyboard._darwin", "pynput.mouse._darwin",
        "Quartz", "AppKit", "ApplicationServices", "Foundation",
    ]

a = Analysis(
    [os.path.join(ROOT, "desktop", "trace_node_entry.py")],
    pathex=[ROOT],
    binaries=[],
    # The desktop node prefers the CLOUD copy of the UI so a deployed front-end fix
    # reaches an already-installed app; this bundled copy is the fallback it serves
    # when the cloud is slow, cold or unreachable. Both dirs are tiny.
    datas=[
        (os.path.join(ROOT, "trace", "ui", "app_static"), "trace/ui/app_static"),
        (os.path.join(ROOT, "trace", "ui", "static"), "trace/ui/static"),
    ],
    hiddenimports=[
        "pynput", "pynput.keyboard", "pynput.mouse",
        "certifi",
    ] + CAPTURE_IMPORTS,
    hookspath=[],
    runtime_hooks=[],
    # Cloud-only deps: imported lazily and only on proxied code paths, so the
    # desktop node never needs them. (Note: `yaml` is imported at app module top,
    # so it must NOT be excluded.)
    excludes=["anthropic", "psycopg", "psycopg_binary", "supabase", "supabase_auth",
              "postgrest", "storage3", "supafunc", "realtime"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# onedir mode: EXE is just the bootloader + script; COLLECT gathers everything
# into dist/trace-node/ as loose files. No decompression on launch.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="trace-node",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="trace-node",
)
