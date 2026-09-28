# Trace Desktop App (macOS and Windows) — Build & Run

The desktop app is a thin **Tauri** shell that:
- launches the bundled **Python capture node** (`trace-node`, a frozen build of
  `trace.ui.app` running in *desktop mode*),
- shows the **end-user UI** the node serves at `http://127.0.0.1:<port>/app`,
- runs **capture locally** and **proxies auth + the pipeline to the cloud
  backend**, uploading the captured `raw_events.jsonl` to the cloud on Stop.

No secrets ship in the app. Your cloud (Phase 1, Render) holds the Anthropic +
Pipedream credentials and runs the pipeline. The user just signs in.

> Release builds are **signed with a Developer ID and notarized**. Users grant the
> macOS capture permissions once. Staple the notarization ticket before making the
> DMG (see "Sign, notarize, staple" below). Do NOT ad-hoc re-sign a notarized
> bundle — that replaces the Developer ID signature and breaks notarization.

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

## Installing (what the user/co-founder does)
1. Open the `.dmg`, drag the app to **Applications**.
2. First launch: double-click. A signed, notarized, stapled build opens without a
   Gatekeeper dialog. If macOS says the app "cannot be opened" or "is damaged", the
   build was not stapled or the signature was altered after notarization — check it
   with `spctl -a -vv /Applications/Trace.app` (must say `source=Notarized Developer
   ID`) rather than telling testers to bypass Gatekeeper.
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

## Windows

Same app, same code: a separate installer built from this repo. The cloud side —
workflow finding, agents, approvals — is identical; only capture and packaging
differ.

**Build:** GitHub Actions does it (`.github/workflows/windows-build.yml`, on every
push to `windows-port` or by hand from the Actions tab → *windows-build* → *Run
workflow*). The run's **trace-windows-installer** artifact is the installer,
`Trace_<version>_x64-setup.exe`. On a Windows PC by hand it is the Mac steps with
Windows paths: `pyinstaller desktop/trace-node.spec`, copy `dist/trace-node` to
`src-tauri/binaries/trace-node`, then `cargo tauri build`. `tauri.windows.conf.json`
is layered over `tauri.conf.json` automatically and switches the output to an NSIS
installer.

**What the run checks:** the capture tests; a live recording against Notepad through
the real `CaptureController` (`desktop/windows_smoke.py` — also runnable by hand on
any Windows PC); and that the *packaged* node still reaches UI Automation, which is
the failure packaging would otherwise hide.

**How capture differs:**
- UI Automation (`trace/capture/uia.py`) stands in for the macOS Accessibility API
  behind the same adapter interface, so `ax.py`'s logic is shared.
- No permission prompts at all. The one limit: apps run **as Administrator** cannot
  be read, and record as window titles only.
- Window titles and app names come from the window manager (`win_window.py`); the
  clipboard is read only when Windows says it changed (`clipboard.py`).

**Installing (tester):** run the installer — per-user, no admin rights needed. Until
the installer is code-signed, SmartScreen warns: **More info → Run anyway**.
Antivirus or company security software may flag an app that records keystrokes;
that is expected for this category of app and is what signing mitigates. Then load
the Chrome extension exactly as on macOS.

**Logs:** `%APPDATA%\com.trace.desktop\logs\trace-node.log`.

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

## Sign, notarize, staple (release builds)

Sign with the Developer ID certificate (Tauri does this when `signingIdentity` is
set in `tauri.conf.json` or `APPLE_SIGNING_IDENTITY` is exported), submit for
notarization, then **staple the ticket into the bundle before building the DMG**:

```bash
BUNDLE=src-tauri/target/release/bundle/macos/Trace.app
xcrun stapler staple "$BUNDLE"
xcrun stapler validate "$BUNDLE"        # must say: The validate action worked
spctl -a -vv "$BUNDLE"                  # must say: source=Notarized Developer ID
```

Without stapling, every launch asks Apple's servers to verify the ticket; on a
flaky or locked-down network the tester gets a hard "damaged / cannot be opened"
failure with no bypass.

### Why permission grants went stale (history, and the one-time migration)

Earlier builds were ad-hoc signed (`codesign --sign -`). macOS keys every
permission row on bundle id **plus** code signature, so each rebuild changed the
signature and orphaned the row: System Settings showed Trace ticked under
Accessibility while the app reported `granted: false` forever. Diagnosed
2026-08-19 after several wasted capture sessions.

A Developer ID signature is stable across builds, so this stops recurring. Testers
who had an ad-hoc build still carry a dead row under the same bundle id. Either
change the `identifier` in `tauri.conf.json` (clean slate for everyone; note it also
moves the app data folder under `~/Library/Application Support/`, orphaning locally
stored sessions — cloud sessions are unaffected) or have them run once:

```bash
tccutil reset Accessibility com.trace.desktop
```

then relaunch and grant fresh. Verify what macOS is actually honouring with:

```bash
grep "reported port" ~/Library/Application\ Support/com.trace.desktop/logs/trace-node.log
curl -s http://127.0.0.1:<port>/api/permissions | python -m json.tool
```
