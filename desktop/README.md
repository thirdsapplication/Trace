# Trace Desktop App (macOS) — Build & Run

The desktop app is a thin **Tauri** shell that:
- launches the bundled **Python capture node** (`trace-node`, a frozen build of
  `trace.ui.app` running in *desktop mode*),
- shows the **end-user UI** the node serves at `http://127.0.0.1:<port>/app`,
- runs **capture locally** and **proxies auth + the pipeline to the cloud
  backend**, uploading the captured `raw_events.jsonl` to the cloud on Stop.

No secrets ship in the app. Your cloud (Phase 1, Render) holds the Anthropic +
Pipedream credentials and runs the pipeline. The user just signs in.

> This build is **unsigned** (Apple signing/notarization is deferred). Users do a
> one-time Gatekeeper override and grant macOS capture permissions once.

---

## Prerequisites (build machine = a Mac)
- **Python 3.12** + the repo deps: `pip install -r requirements.txt` and
  `pip install pyinstaller`.
- **Rust** (stable) + **Tauri v2** prerequisites: `cargo install tauri-cli` (or use
  `npm`/`cargo tauri`). See https://tauri.app for the current toolchain.
- An app icon at `src-tauri/icons/icon.icns` (Tauri can generate a set with
  `cargo tauri icon path/to/logo.png`).

## 1. Set the cloud backend URL
Edit `DEFAULT_CLOUD_BACKEND` in `src-tauri/src/main.rs` to your Render service URL
(e.g. `https://trace-backend.onrender.com`). It is overridable at runtime via the
`TRACE_CLOUD_BACKEND` environment variable (handy for pointing the same app at a
**local** backend during dev — see "Dual-run" below).

## 2. Build the Python sidecar
From the **repo root**:
```bash
pyinstaller desktop/trace-node.spec      # → dist/trace-node/ (onedir bundle)
```
The shell resolves the sidecar at `<resources>/binaries/trace-node/trace-node`
and `tauri.conf.json` bundles `binaries/trace-node/**/*`, so copy the whole
onedir output — directory name and all, **no target-triple suffix**:
```bash
mkdir -p src-tauri/binaries
cp -R dist/trace-node src-tauri/binaries/trace-node
```

## 3. Build the app
```bash
cargo tauri build       # or: npm run tauri build
```
Output (unsigned) under `src-tauri/target/release/bundle/`:
- `dmg/Trace_0.1.0_<arch>.dmg` — the distributable
- `macos/Trace.app` — the app bundle

## 4. Develop without rebuilding the bundle
```bash
cargo tauri dev
```
Make sure `src-tauri/binaries/trace-node/` exists first (dev still spawns
the sidecar).

## Troubleshooting a startup that won't finish
The shell supervises the node: it waits (indefinitely, while the node is alive)
for the node's portfile and a passing `/health`, restarts a node that dies during
startup, and reports any hard failure on the splash screen together with the log
path. Everything both sides print lands in one per-launch log:
```
~/Library/Application Support/com.trace.desktop/logs/trace-node.log
```
To simulate a slow start (and watch the splash status escalate instead of
hanging), launch with `TRACE_STARTUP_DELAY_S=30`.

---

## Installing the unsigned build (what the user/co-founder does)
1. Open the `.dmg`, drag **Trace.app** to **Applications**.
2. First launch: double-click it, dismiss the "cannot be opened" dialog, then open
   System Settings → Privacy & Security, scroll to the bottom and click **Open
   Anyway** (macOS asks for your password or Touch ID). **Right-click → Open no
   longer bypasses Gatekeeper on macOS 15.1 and later.** If macOS says the app "is
   damaged", the quarantine flag is the cause; clear it and try again:
   `xattr -cr /Applications/Trace.app`
3. Grant capture permissions when prompted (or proactively in System Settings →
   Privacy & Security). The pane names depend on the macOS release:
   - **Accessibility** → enable **Trace** (focused element, clicked control, window
     title). **On macOS 27 this pane is called "Device Control and Data Access"** and
     is no longer under Privacy & Security — search System Settings for it. This is
     the one that matters most; the app's Record screen can trigger the prompt.
   - **Input Monitoring** → enable **Trace** (keystrokes and clicks, `pynput`). macOS
     never prompts for this one; add it by hand, then quit and relaunch.
   - **Screen Recording** (called **Screen & System Audio Recording** on macOS 26 and
     later) → optional; window titles come from Accessibility when it is off. Fully
     quit and relaunch after granting it.
   The grants attach to `Trace.app`; the sidecar inherits them as a child process.
   The app reports what macOS is actually honouring at `/api/permissions` (see the
   verify commands at the end of this file) — trust that over the ticked switches.
4. Load the **Chrome extension** (browser-DOM half of capture): `chrome://extensions`
   → enable **Developer mode** → **Load unpacked** → select `trace/extension/`.
5. **Sign in** (cloud account), **Record** a short bit of work, then run the
   pipeline — capture uploads to the cloud on Stop and reconstruct/discover run there.

---

## Dual-run (cloud vs local backend)
The node is switched only by configuration:
- **Cloud (default):** `TRACE_CLOUD_BACKEND` = your Render URL (baked into the
  shell, overridable by env).
- **Local backend for dev:** run a backend with `python -m trace.ui.app`
  (`PORT=8765`), then launch the shell with
  `TRACE_CLOUD_BACKEND=http://127.0.0.1:8765 cargo tauri dev`.
- **No desktop shell at all:** the existing `python -m trace.ui.app` (no
  `TRACE_CLOUD_BACKEND`) is completely unchanged.

## How it fits together
```
Tauri shell ──spawns──▶ trace-node sidecar (127.0.0.1:<port>, desktop mode)
   │                         │  capture/* + /health + /permissions: LOCAL
   └─ webview ─▶ /app        │  /app UI assets: LOCAL (bundled; TRACE_DESKTOP_LOCAL_UI=0 re-proxies)
                             │  everything else (auth, pipeline): PROXY ─▶ Render cloud
                             └─ on capture Stop: POST raw_events.jsonl ─▶ cloud (new session)
```

## Re-sign after every build (REQUIRED on macOS)

`cargo tauri build` emits a bundle whose signature claims sealed resources while
sealing none:

    /Applications/Trace.app: code has no resources but signature indicates they must be present

macOS silently refuses to honour ANY permission grant for an app whose signature
does not verify. The symptom is brutal to debug: System Settings shows Trace
ticked under Accessibility, and the app still reports `granted: false` forever.
Diagnosed 2026-08-19 after several wasted capture sessions.

Run these after every build, before installing:

```bash
BUNDLE=src-tauri/target/release/bundle/macos/Trace.app
xattr -cr "$BUNDLE"                       # extended attributes block signing
codesign --force --deep --sign - "$BUNDLE"
codesign --verify --deep --strict "$BUNDLE"   # must say "valid on disk"
```

`xattr -cr` is not optional — without it codesign fails with "resource fork,
Finder information, or similar detritus not allowed".

Re-signing changes the code hash, so every existing TCC entry goes stale: remove
Trace from Accessibility / Screen Recording / Input Monitoring with the minus
button, re-add it, and relaunch. Verify with:

```bash
grep "reported port" ~/Library/Application\ Support/com.trace.desktop/logs/trace-node.log
curl -s http://127.0.0.1:<port>/api/permissions | python -m json.tool
```

A Developer ID signature removes this whole dance permanently and is the real fix.
