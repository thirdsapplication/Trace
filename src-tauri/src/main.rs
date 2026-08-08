// Trace desktop shell (Tauri v2).
//
// Responsibilities:
//   1. Launch and supervise the bundled Python capture node (the `trace-node`
//      sidecar = the existing `trace.ui.app` server in desktop mode).
//   2. Point the webview at that node's localhost origin, so the end-user UI —
//      served/proxied by the node — talks to a single origin (cookies + relative
//      URLs "just work"; capture is local, pipeline is proxied to the cloud).
//   3. Tear the sidecar down on exit.
//
// The node's backend target is configuration, not hardcoded: TRACE_CLOUD_BACKEND
// (env) overrides DEFAULT_CLOUD_BACKEND below, so the same app points at the
// cloud by default and at a local backend for dev with zero code change.
//
// NOTE: built and verified on macOS; exact Tauri v2 API names can drift across
// minor versions — adjust against the installed tauri crate if compilation flags
// a renamed item (see desktop/README.md).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpListener;
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

// EDIT ME: the product owner's Render backend URL. Overridable at runtime via the
// TRACE_CLOUD_BACKEND environment variable. Public URL — never a secret.
const DEFAULT_CLOUD_BACKEND: &str = "https://trace-backend-6nd8.onrender.com";

// Holds the running sidecar so we can kill it on exit.
struct Sidecar(Mutex<Option<CommandChild>>);

// Open a URL in the user's default system browser. The end-user UI (served from
// the cloud) calls this via window.__TAURI__.core.invoke("open_external", { url })
// because window.open() to an external https URL is blocked inside the webview —
// this is how the Pipedream Connect / Google authorization page gets opened.
#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    opener::open(&url).map_err(|e| e.to_string())
}

fn pick_free_port() -> u16 {
    // Bind to :0, read the assigned port, then drop the listener so the sidecar
    // can take it. A momentary race window, but fine for a single local app.
    TcpListener::bind("127.0.0.1:0")
        .expect("no free port")
        .local_addr()
        .unwrap()
        .port()
}

fn cloud_backend() -> String {
    std::env::var("TRACE_CLOUD_BACKEND").unwrap_or_else(|_| DEFAULT_CLOUD_BACKEND.to_string())
}

fn wait_until_ready(port: u16) {
    // The node binds then serves immediately, so a successful TCP connect means
    // the HTTP server (incl. /health) is up. Poll briefly with a hard cap.
    use std::net::TcpStream;
    for _ in 0..100 {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            std::thread::sleep(Duration::from_millis(250)); // let routes settle
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![open_external])
        .manage(Sidecar(Mutex::new(None)))
        .setup(|app| {
            let port = pick_free_port();

            // Per-user writable session dir for locally-captured raw streams.
            let sessions_root = app
                .path()
                .app_data_dir()
                .map(|d| d.join("sessions"))
                .unwrap_or_else(|_| std::env::temp_dir().join("trace-sessions"));
            let _ = std::fs::create_dir_all(&sessions_root);

            // Spawn the frozen Python node. HOST/PORT/TRACE_CLOUD_BACKEND/
            // TRACE_SESSIONS_ROOT come via env; --no-open suppresses the browser.
            let (_rx, child) = app
                .shell()
                .sidecar("trace-node")
                .expect("trace-node sidecar missing from bundle")
                .env("HOST", "127.0.0.1")
                .env("PORT", port.to_string())
                .env("TRACE_CLOUD_BACKEND", cloud_backend())
                .env("TRACE_SESSIONS_ROOT", sessions_root.to_string_lossy().to_string())
                .args(["--no-open"])
                .spawn()
                .expect("failed to start trace-node sidecar");
            app.state::<Sidecar>().0.lock().unwrap().replace(child);

            // Once the node answers, point the window at the end-user UI it serves.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                wait_until_ready(port);
                if let Some(win) = handle.get_webview_window("main") {
                    let url = format!("http://127.0.0.1:{}/app", port);
                    let _ = win.navigate(url.parse().unwrap());
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Trace")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                // Best-effort: stop capture cleanly and reap the sidecar.
                if let Some(child) = app_handle.state::<Sidecar>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}
