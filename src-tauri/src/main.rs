// Trace desktop shell (Tauri v2).
//
// Responsibilities:
//   1. Launch and supervise the bundled Python capture node (the `trace-node`
//      onedir bundle = the existing `trace.ui.app` server in desktop mode).
//   2. Point the webview at that node's localhost origin, so the end-user UI —
//      served/proxied by the node — talks to a single origin (cookies + relative
//      URLs "just work"; capture is local, pipeline is proxied to the cloud).
//   3. Tear the sidecar down on exit.
//
// Startup contract: the node binds port 0 itself and publishes the real port via
// a portfile (TRACE_PORTFILE) — the shell never picks a port, so there is no
// window where another process can steal it. The shell then waits, with no
// deadline while the node is alive, for an HTTP 200 from /health that identifies
// as trace-backend, navigates, and verifies the navigation took. A node that dies
// during startup is restarted (bounded), and every failure ends as a visible
// error + log path on the splash — never a silent, eternal "Starting…".
//
// The node's backend target is configuration, not hardcoded: TRACE_CLOUD_BACKEND
// (env) overrides DEFAULT_CLOUD_BACKEND below, so the same app points at the
// cloud by default and at a local backend for dev with zero code change.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, RunEvent, Url};

// EDIT ME: the product owner's Render backend URL. Overridable at runtime via the
// TRACE_CLOUD_BACKEND environment variable. Public URL — never a secret.
const DEFAULT_CLOUD_BACKEND: &str = "https://trace-backend-6nd8.onrender.com";

// A node that exits during startup is respawned this many times before giving up.
const SPAWN_ATTEMPTS: u32 = 3;
// A navigation that doesn't take (webview still on the splash) is retried this
// many times before the splash shows a fatal error.
const NAVIGATE_ATTEMPTS: u32 = 5;

// Holds the running sidecar so we can kill it on exit.
struct Sidecar(Mutex<Option<Child>>);

// Open a URL in the user's default system browser. The end-user UI (served from
// the cloud) calls this via window.__TAURI__.core.invoke("open_external", { url })
// because window.open() to an external https URL is blocked inside the webview —
// this is how the Pipedream Connect / Google authorization page gets opened.
#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    opener::open(&url).map_err(|e| e.to_string())
}

fn cloud_backend() -> String {
    std::env::var("TRACE_CLOUD_BACKEND").unwrap_or_else(|_| DEFAULT_CLOUD_BACKEND.to_string())
}

// ---------------------------------------------------------------------------
// Startup log: one file per launch, shared by the shell's own lifecycle lines
// and the node's stdout/stderr (which are invisible inside a .app otherwise).
// ---------------------------------------------------------------------------

struct StartupLog {
    file: Mutex<Option<File>>,
    path: PathBuf,
    t0: Instant,
}

impl StartupLog {
    fn new(path: PathBuf) -> Self {
        let _ = fs::write(&path, ""); // one launch per file
        let file = OpenOptions::new().create(true).append(true).open(&path).ok();
        StartupLog { file: Mutex::new(file), path, t0: Instant::now() }
    }

    fn line(&self, msg: &str) {
        if let Ok(mut guard) = self.file.lock() {
            if let Some(f) = guard.as_mut() {
                let _ = writeln!(f, "[shell +{:.1}s] {}", self.t0.elapsed().as_secs_f64(), msg);
            }
        }
    }

    // A dup of the log handle for the child's stdout/stderr. Dup'd descriptors
    // share the append-mode file description, so shell and node lines interleave
    // instead of clobbering.
    fn stdio(&self) -> Stdio {
        self.file
            .lock()
            .ok()
            .and_then(|g| g.as_ref().and_then(|f| f.try_clone().ok()))
            .map(Stdio::from)
            .unwrap_or_else(Stdio::null)
    }
}

// ---------------------------------------------------------------------------
// Splash updates. The splash page defines window.__setStatus; after navigation
// to the real UI the guard makes these evals a no-op. No IPC — no capability
// config exists, so eval-from-Rust is the only channel that works everywhere.
// ---------------------------------------------------------------------------

fn splash_eval(handle: &AppHandle, payload: &serde_json::Value) {
    if let Some(win) = handle.get_webview_window("main") {
        let _ = win.eval(&format!("window.__setStatus && window.__setStatus({})", payload));
    }
}

fn splash_status(handle: &AppHandle, text: &str) {
    splash_eval(handle, &serde_json::json!({ "text": text }));
}

// A fatal error is re-evaled for a few seconds: a very early failure can beat
// the splash page's own load, and a lost eval here would mean a silent hang —
// the exact bug this shell is built not to have.
fn splash_fatal(handle: &AppHandle, log: &StartupLog, msg: &str) {
    log.line(&format!("FATAL: {}", msg));
    let payload = serde_json::json!({ "error": msg, "logPath": log.path.to_string_lossy() });
    let js = format!("window.__setStatus && window.__setStatus({})", payload);
    let handle = handle.clone();
    std::thread::spawn(move || {
        for _ in 0..10 {
            if let Some(win) = handle.get_webview_window("main") {
                let _ = win.eval(&js);
            }
            std::thread::sleep(Duration::from_millis(500));
        }
    });
}

// Escalate the splash text while a wait drags on, so a slow (but healthy) start
// reads as progress rather than a hang.
struct Escalator {
    t0: Instant,
    said_10: bool,
    said_30: bool,
}

impl Escalator {
    fn new() -> Self {
        Escalator { t0: Instant::now(), said_10: false, said_30: false }
    }

    fn tick(&mut self, handle: &AppHandle) {
        let s = self.t0.elapsed().as_secs();
        if s >= 30 && !self.said_30 {
            self.said_30 = true;
            splash_status(handle, "Still starting… a first launch can take a minute.");
        } else if s >= 10 && !self.said_10 {
            self.said_10 = true;
            splash_status(handle, "Still starting…");
        }
    }
}

// ---------------------------------------------------------------------------
// Sidecar lifecycle
// ---------------------------------------------------------------------------

fn spawn_node(
    node_exe: &Path,
    cloud: &str,
    sessions_root: &Path,
    portfile: &Path,
    log: &StartupLog,
) -> std::io::Result<Child> {
    Command::new(node_exe)
        .env("HOST", "127.0.0.1")
        .env("PORT", "0") // the node binds :0 itself and reports via the portfile
        .env("TRACE_PORTFILE", portfile)
        .env("TRACE_CLOUD_BACKEND", cloud)
        .env("TRACE_SESSIONS_ROOT", sessions_root)
        .env("PYTHONUNBUFFERED", "1")
        .arg("--no-open")
        .stdout(log.stdio())
        .stderr(log.stdio())
        .spawn()
}

// Some(exit description) if the sidecar is no longer running.
fn child_exited(handle: &AppHandle) -> Option<String> {
    let state = handle.state::<Sidecar>();
    let mut guard = state.0.lock().unwrap_or_else(|p| p.into_inner());
    match guard.as_mut() {
        None => Some("not running".to_string()),
        Some(child) => match child.try_wait() {
            Ok(Some(status)) => Some(status.to_string()),
            Ok(None) => None,
            Err(e) => Some(format!("try_wait failed: {}", e)),
        },
    }
}

// Stage 1: the node publishes its self-assigned port. No deadline — the node is
// supervised, so a child that died is the exit path, not a timer.
fn wait_for_portfile(
    handle: &AppHandle,
    portfile: &Path,
    esc: &mut Escalator,
) -> Result<u16, String> {
    loop {
        if let Ok(s) = fs::read_to_string(portfile) {
            if let Ok(p) = s.trim().parse::<u16>() {
                if p != 0 {
                    return Ok(p);
                }
            }
        }
        if let Some(status) = child_exited(handle) {
            return Err(format!("trace-node exited before publishing its port ({})", status));
        }
        esc.tick(handle);
        std::thread::sleep(Duration::from_millis(100));
    }
}

// One raw-HTTP probe of /health. Requires a 200 whose body identifies as
// trace-backend, so a foreign process squatting on a recycled port can't pass.
fn health_ok(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_secs(2)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(2)));
    let req = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        port
    );
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = Vec::new();
    let _ = s.read_to_end(&mut buf); // a timeout still leaves any partial body in buf
    let text = String::from_utf8_lossy(&buf);
    (text.starts_with("HTTP/1.0 200") || text.starts_with("HTTP/1.1 200"))
        && text.contains("trace-backend")
}

// Stage 2: poll /health until it answers. Same no-deadline-while-alive rule.
fn wait_for_health(handle: &AppHandle, port: u16, esc: &mut Escalator) -> Result<(), String> {
    loop {
        if health_ok(port) {
            return Ok(());
        }
        if let Some(status) = child_exited(handle) {
            return Err(format!("trace-node exited before becoming healthy ({})", status));
        }
        esc.tick(handle);
        std::thread::sleep(Duration::from_millis(250));
    }
}

// Stage 3: navigate and verify the webview actually left the splash. A failed
// provisional navigation (node not quite ready, transient refusal) keeps the
// current document with no error — so navigate is retried until win.url() shows
// the localhost origin.
fn navigate_to_app(handle: &AppHandle, port: u16, log: &StartupLog) -> Result<(), String> {
    let url: Url = format!("http://127.0.0.1:{}/app", port)
        .parse()
        .map_err(|e| format!("bad app URL: {}", e))?;
    for attempt in 1..=NAVIGATE_ATTEMPTS {
        let Some(win) = handle.get_webview_window("main") else {
            return Err("main window is gone".to_string());
        };
        match win.navigate(url.clone()) {
            Ok(()) => log.line(&format!("navigate attempt {}: issued {}", attempt, url)),
            Err(e) => log.line(&format!("navigate attempt {}: error: {}", attempt, e)),
        }
        std::thread::sleep(Duration::from_secs(1));
        if let Some(win) = handle.get_webview_window("main") {
            match win.url() {
                Ok(current) if current.scheme().starts_with("http") && current.port() == Some(port) => {
                    return Ok(());
                }
                Ok(current) => log.line(&format!("webview still on {} after attempt {}", current, attempt)),
                Err(e) => log.line(&format!("webview url() failed after attempt {}: {}", attempt, e)),
            }
        }
    }
    Err(format!("webview never reached {} after {} attempts", url, NAVIGATE_ATTEMPTS))
}

// The whole supervised startup: spawn → portfile → health → navigate, restarting
// a node that dies mid-startup, and ending every failure on the splash with the
// log path instead of leaving "Starting…" up forever.
fn run_node(
    handle: AppHandle,
    node_exe: PathBuf,
    sessions_root: PathBuf,
    portfile: PathBuf,
    log: StartupLog,
) {
    let cloud = cloud_backend();
    log.line(&format!("node exe: {}", node_exe.display()));
    log.line(&format!("cloud backend: {}", cloud));

    for attempt in 1..=SPAWN_ATTEMPTS {
        let _ = fs::remove_file(&portfile); // never trust a stale port
        if attempt > 1 {
            splash_status(&handle, "Restarting…");
            std::thread::sleep(Duration::from_secs(1));
        }
        log.line(&format!("spawn attempt {}/{}", attempt, SPAWN_ATTEMPTS));
        let child = match spawn_node(&node_exe, &cloud, &sessions_root, &portfile, &log) {
            Ok(c) => c,
            Err(e) => {
                splash_fatal(&handle, &log, &format!(
                    "Trace couldn't start its background service ({}). Try reinstalling the app.", e));
                return;
            }
        };
        {
            let state = handle.state::<Sidecar>();
            let mut guard = state.0.lock().unwrap_or_else(|p| p.into_inner());
            if let Some(mut old) = guard.replace(child) {
                let _ = old.kill();
            }
        }

        let mut esc = Escalator::new();
        let port = match wait_for_portfile(&handle, &portfile, &mut esc) {
            Ok(p) => p,
            Err(e) => {
                log.line(&e);
                continue;
            }
        };
        log.line(&format!("node reported port {}", port));

        if let Err(e) = wait_for_health(&handle, port, &mut esc) {
            log.line(&e);
            continue;
        }
        log.line("health check passed");

        match navigate_to_app(&handle, port, &log) {
            Ok(()) => {
                log.line("navigated to app");
                return;
            }
            Err(e) => {
                splash_fatal(&handle, &log, &format!(
                    "Trace started, but its window failed to load ({}).", e));
                return;
            }
        }
    }
    splash_fatal(&handle, &log,
        "Trace's background service keeps exiting during startup.");
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![open_external])
        .manage(Sidecar(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();

            // Per-user writable state: captured sessions, the startup log, and
            // the port handoff file all live under the app data dir.
            let data_dir = app
                .path()
                .app_data_dir()
                .unwrap_or_else(|_| std::env::temp_dir().join("trace-desktop"));
            let sessions_root = data_dir.join("sessions");
            let logs_dir = data_dir.join("logs");
            let _ = fs::create_dir_all(&sessions_root);
            let _ = fs::create_dir_all(&logs_dir);
            let log = StartupLog::new(logs_dir.join("trace-node.log"));
            let portfile = data_dir.join("node.port");

            // Resolve the trace-node executable inside the bundled resources.
            // In a .app bundle: Contents/Resources/binaries/trace-node/trace-node
            let node_exe = match app.path().resource_dir() {
                Ok(d) => d.join("binaries").join("trace-node").join("trace-node"),
                Err(e) => {
                    // The window exists; report on the splash instead of panicking.
                    splash_fatal(&handle, &log,
                        &format!("Trace couldn't locate its bundled files ({}).", e));
                    return Ok(());
                }
            };

            std::thread::spawn(move || run_node(handle, node_exe, sessions_root, portfile, log));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Trace")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                let state = app_handle.state::<Sidecar>();
                let mut guard = state.0.lock().unwrap_or_else(|p| p.into_inner());
                if let Some(mut child) = guard.take() {
                    let _ = child.kill();
                }
            }
        });
}
