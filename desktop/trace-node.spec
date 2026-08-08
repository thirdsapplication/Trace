# PyInstaller spec for the Trace desktop capture node (macOS sidecar).
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
# bundle small. pynput + pyobjc are macOS capture deps and MUST be bundled.

import os

# The spec runs with its own dir as CWD-ish; point pathex at the repo root so the
# `trace` package and its data dirs resolve from a clean clone.
ROOT = os.path.abspath(os.path.join(os.path.dirname(SPECPATH), "."))

block_cipher = None

a = Analysis(
    [os.path.join(ROOT, "desktop", "trace_node_entry.py")],
    pathex=[ROOT],
    binaries=[],
    # Static UI is proxied from the cloud in desktop mode, but bundle it anyway so
    # nothing breaks if a local static route is ever hit; both dirs are tiny.
    datas=[
        (os.path.join(ROOT, "trace", "ui", "app_static"), "trace/ui/app_static"),
        (os.path.join(ROOT, "trace", "ui", "static"), "trace/ui/static"),
    ],
    # PyInstaller frequently misses pynput's platform backend and the pyobjc
    # submodules used by capture (window titles) and permissions checks.
    hiddenimports=[
        "pynput", "pynput.keyboard", "pynput.mouse",
        "pynput.keyboard._darwin", "pynput.mouse._darwin",
        "Quartz", "AppKit", "ApplicationServices", "Foundation",
        "certifi",
    ],
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
