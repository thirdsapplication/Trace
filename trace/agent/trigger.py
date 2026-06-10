"""P7 — trigger generation from observed instances.

Trigger messages are generated from the bundle's own data (its edited trigger text +
the real input artifacts mounted for the run), never hand-written per bundle and
never referencing a fixture string. Where the bundle has several input artifacts
(distinct observed instances), one trigger is produced per input — the Deployer runs
each as its own session (capped by ``max_sessions``).
"""

import os

from . import config


def generate_triggers(bundle, env_record, mount_path=None, max_sessions=None):
    mount = (mount_path or config.MOUNT_PATH).rstrip("/")
    max_sessions = max_sessions or config.DEFAULT_MAX_SESSIONS
    base = (bundle.get("trigger") or "").strip() or "A new task arrived for this workflow."

    inputs = [a for a in (env_record or {}).get("artifacts", [])
              if a.get("uploaded") and a.get("file_ref") and a.get("role") in ("input", "reference")]
    if not inputs:
        return [{"message": f"{base}\n\nExecute the workflow per your spec.", "input_files": []}]

    triggers = []
    for a in inputs[:max_sessions]:
        fn = os.path.basename(a["file_ref"])
        instr = (a.get("user_instruction") or "").strip()
        msg = (f"{base}\n\nThe input for this instance is the file mounted at "
               f"`{mount}/{fn}`" + (f" — {instr}" if instr else "") + ".\n\n"
               "Execute the workflow per your spec, reading that file.")
        triggers.append({"message": msg, "input_files": [fn]})
    return triggers
