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
pyinstaller desktop/trace-node.spec      # → dist/trace-node
```
Tauri expects the sidecar named with the Rust target triple. Copy it in:
```bash
mkdir -p src-tauri/binaries
# Apple Silicon:
cp dist/trace-node src-tauri/binaries/trace-node-aarch64-apple-darwin
# Intel:
# cp dist/trace-node src-tauri/binaries/trace-node-x86_64-apple-darwin
```
(Find your triple with `rustc -Vv | grep host`.)

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
Make sure `src-tauri/binaries/trace-node-<triple>` exists first (dev still spawns
the sidecar).

---

## Installing the unsigned build (what the user/co-founder does)
1. Open the `.dmg`, drag **Trace.app** to **Applications**.
2. First launch: **right-click → Open** (or System Settings → Privacy & Security →
   **Open Anyway**) to clear the one-time Gatekeeper quarantine.
3. Grant capture permissions when prompted (or proactively in System Settings →
   Privacy & Security):
   - **Input Monitoring** → enable **Trace** (keystroke capture, `pynput`)
   - **Accessibility** → enable **Trace** (active-window titles, `osascript`)
   The grants attach to `Trace.app`; the sidecar inherits them as a child process.
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
   └─ webview ─▶ /app        │  everything else (auth, /app, pipeline): PROXY ─▶ Render cloud
                             └─ on capture Stop: POST raw_events.jsonl ─▶ cloud (new session)
```
