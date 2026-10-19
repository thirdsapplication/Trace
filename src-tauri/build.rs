fn main() {
    // Declare the shell's own commands so a capability can grant them. The UI comes
    // from the local node (a remote origin to Tauri), and remote origins get only
    // what a capability lists — see capabilities/local-node.json.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["open_external", "restart_app"]),
    ))
    .expect("failed to run tauri-build");
}
