"""Local test-harness backend for Engine 1 (handoff §7).

A small stdlib HTTP server (no framework, no new deps — consistent with the
collector) that drives the *existing* engine: it manages capture via the §6
control surface, runs the existing reconstruction pipeline (real + dry-run), and
serves the three engine artifacts as JSON for inspection. The front end is a
single static page served from ``ui/static/``.

LOCAL ONLY (handoff §3): binds 127.0.0.1, no accounts, no remote anything.

Seams kept injectable so the whole backend is testable without a key, pynput, or
network: ``AppState.reconstruct_fn`` and ``AppState.controller_factory``.
"""

import argparse
import json
import os
import re
import shutil
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from ..capture.controller import CaptureController
from ..reconstruct import chunking
from ..reconstruct.prompt import SYSTEM_PROMPT
from ..reconstruct.reconstruct import reconstruct as _real_reconstruct
from ..discovery import scorer as discovery_scorer
from ..discovery.discover import discover as _real_discover
from ..discovery.discover import load_event_log, render_event_log
from ..discovery.prompt import SYSTEM_PROMPT as DISCOVERY_SYSTEM_PROMPT
from ..artifact import review as artifact_review
from ..artifact.artifact import build_prompt_material, write_spec_bundle
from ..artifact.artifact import generate_specs as _real_generate_specs
from ..artifact.prompt import SYSTEM_PROMPT as ARTIFACT_SYSTEM_PROMPT
from ..agent import configgen as agent_configgen
from ..agent import interview as agent_interview
from ..agent import gate as agent_gate
from ..agent import grader as agent_grader
from . import keystore, permissions, sessions


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _interview_record(bid, questions, answers):
    """Assemble an interview record, computing unresolved + status server-side.
    A question is unresolved iff it has no non-empty answer; zero questions is a
    degenerate 'complete'."""
    answered = {a["q"] for a in answers if str(a.get("answer", "")).strip()}
    unresolved = [q["id"] for q in questions if q["id"] not in answered]
    if not questions:
        status = "complete"
    elif unresolved == [q["id"] for q in questions] and not answers:
        status = "unstarted"
    elif unresolved:
        status = "incomplete"
    else:
        status = "complete"
    return {"bundle_id": bid, "questions": questions, "answers": answers,
            "unresolved": unresolved, "status": status}

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DISCOVERY_FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "discovery", "fixtures")
SHORT_RECORDING_THRESHOLD = 5  # events; below this, warn before a paid run


def list_fixtures():
    """Discover bundled validation fixtures: *_event_log.json files, each with an
    optional matching *_answer_key.json so the gate can be run from the UI."""
    out = []
    if not os.path.isdir(DISCOVERY_FIXTURES_DIR):
        return out
    for fn in sorted(os.listdir(DISCOVERY_FIXTURES_DIR)):
        if not fn.endswith("_event_log.json"):
            continue
        name = fn[: -len("_event_log.json")]
        ak = os.path.join(DISCOVERY_FIXTURES_DIR, f"{name}_answer_key.json")
        out.append({"name": name,
                    "event_log": os.path.join(DISCOVERY_FIXTURES_DIR, fn),
                    "answer_key": ak if os.path.exists(ak) else None})
    return out

_CONTENT_TYPES = {".html": "text/html", ".js": "application/javascript",
                  ".css": "text/css", ".json": "application/json"}


class AppState:
    def __init__(self, sessions_root, collector_port=8137,
                 reconstruct_fn=None, controller_factory=None, key_store=None,
                 discovery_fn=None, score_fn=None, artifact_fn=None,
                 interview_fn=None, config_fn=None, gate_fn=None, cleanup_fn=None):
        self.sessions_root = sessions_root
        self.collector_port = collector_port
        self.reconstruct_fn = reconstruct_fn or _real_reconstruct
        self.discovery_fn = discovery_fn or _real_discover
        self.score_fn = score_fn or discovery_scorer.score
        self.artifact_fn = artifact_fn or _real_generate_specs
        # Phase 2: the interview (gap detector) + the Phase-1 config engine.
        # Phase 3: the hidden logic gate (sandbox run + walled-off grader). All
        # injectable so the build chain runs offline against stubs.
        self.interview_fn = interview_fn or agent_interview.generate_questions
        self.config_fn = config_fn or agent_configgen.compile_config
        self.gate_fn = gate_fn or self._default_gate
        self.cleanup_fn = cleanup_fn or self._default_cleanup
        self.controller_factory = controller_factory or (
            lambda out: CaptureController(out, collector_port=collector_port))
        self.keystore = key_store or keystore
        self.controller = None       # current CaptureController (one session at a time)
        self.current_sid = None
        self.recon_jobs = {}         # sid -> {state, current, total, error, flags, errors}
        self.discover_jobs = {}      # sid -> {state, error, bundles, anti_bundles}
        self.spec_jobs = {}          # sid -> {state, error, built, notes}
        self.interview_jobs = {}     # sid -> {state, error, generated}
        self.build_jobs = {}         # sid -> {state, error, generated, gated, verdicts, errors}
        self.lock = threading.Lock()
        os.makedirs(sessions_root, exist_ok=True)

    def _default_gate(self, bundle, test_dict, criteria, event_log, events_path=None):
        """Real hidden gate: requires a key; builds the beta-pinned client + the
        walled-off Sonnet grader, runs the sandbox session, and grades the transcript.
        Returns a verdict record (advanced iff overall==pass and the run completed)."""
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        from ..agent.api_client import ManagedAgentsClient
        client = ManagedAgentsClient(api_key=key)
        return agent_gate.run_gate(bundle, test_dict, criteria, event_log, client,
                                   judge_fn=agent_grader._default_judge(), events_path=events_path)

    def _default_cleanup(self, session_ids, file_ids, agent_ids):
        """Real, human-initiated platform cleanup: requires a key; builds the client
        and deletes exactly the Trace-created ids it is given."""
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        from ..agent.api_client import ManagedAgentsClient
        client = ManagedAgentsClient(api_key=key)
        return client.cleanup(session_ids=session_ids, file_ids=file_ids, agent_ids=agent_ids)

    # --- reconstruction job ----------------------------------------------
    def start_reconstruction(self, sid):
        raw_path = sessions.path(self.sessions_root, sid, sessions.RAW)
        job = {"state": "running", "current": 0, "total": 0, "error": None,
               "flags": 0, "errors": 0}
        self.recon_jobs[sid] = job

        def run():
            try:
                key = self.keystore.get_key()
                if not key:
                    raise RuntimeError("no_api_key")
                # Supply the key exactly as the engine expects: via the process env.
                os.environ["ANTHROPIC_API_KEY"] = key

                def cb(done, total):
                    job["current"], job["total"] = done, total

                log, report = self.reconstruct_fn(raw_path, progress_cb=cb)
                with open(sessions.path(self.sessions_root, sid, sessions.EVENT_LOG),
                          "w", encoding="utf-8") as f:
                    json.dump(log, f, indent=2, ensure_ascii=False)
                with open(sessions.path(self.sessions_root, sid, sessions.VALIDATION),
                          "w", encoding="utf-8") as f:
                    json.dump({"errors": report.errors, "flags": report.flags},
                              f, indent=2, ensure_ascii=False)
                sessions.update_meta(self.sessions_root, sid,
                                     reconstructed_at=sessions._now(),
                                     event_count=len(log))
                job.update(state="done", flags=len(report.flags),
                           errors=len(report.errors))
            except Exception as e:  # surface real detail (handoff §4.5)
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    # --- discovery job (parallel to reconstruction) ----------------------
    def start_discovery(self, sid):
        log_path = sessions.path(self.sessions_root, sid, sessions.EVENT_LOG)
        job = {"state": "running", "error": None, "bundles": 0, "anti_bundles": 0}
        self.discover_jobs[sid] = job

        def run():
            try:
                key = self.keystore.get_key()
                if not key:
                    raise RuntimeError("no_api_key")
                os.environ["ANTHROPIC_API_KEY"] = key
                discovery, report = self.discovery_fn(log_path)
                with open(sessions.path(self.sessions_root, sid, sessions.DISCOVERY),
                          "w", encoding="utf-8") as f:
                    json.dump(discovery, f, indent=2, ensure_ascii=False)
                sessions.update_meta(self.sessions_root, sid,
                                     discovered_at=sessions._now())
                job.update(state="done", bundles=len(discovery["bundles"]),
                           anti_bundles=len(discovery["anti_bundles"]))
            except Exception as e:
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    # --- spec generation job (Stage 3) -----------------------------------
    def start_spec_generation(self, sid):
        reviewed_path = sessions.path(self.sessions_root, sid, sessions.REVIEWED)
        event_log_path = sessions.path(self.sessions_root, sid, sessions.EVENT_LOG)
        job = {"state": "running", "error": None, "built": 0, "notes": ""}
        self.spec_jobs[sid] = job

        def run():
            try:
                # only the build path needs the model/key; anti/reject do not
                with open(reviewed_path, encoding="utf-8") as rf:
                    reviewed = json.load(rf)
                approved = sum(1 for b in (reviewed.get("bundles") or [])
                               if b.get("disposition") == "approve_for_build"
                               and b.get("classification", "bundle") == "bundle")
                key = self.keystore.get_key()
                if approved and not key:
                    raise RuntimeError("no_api_key")
                if key:
                    os.environ["ANTHROPIC_API_KEY"] = key

                results, report = self.artifact_fn(reviewed_path, event_log_path)
                specs_dir = sessions.path(self.sessions_root, sid, sessions.SPECS_DIR)
                if os.path.isdir(specs_dir):    # clear stale specs + their _files/ bundles
                    shutil.rmtree(specs_dir)
                os.makedirs(specs_dir, exist_ok=True)
                built = 0
                for r in results:
                    write_spec_bundle(specs_dir, r)   # writes spec_<id>.md + spec_<id>_files/
                    if r.get("built"):
                        built += 1
                sessions.update_meta(self.sessions_root, sid, specs_generated_at=sessions._now())
                job.update(state="done", built=built, notes=str(report))
            except Exception as e:
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    # --- Build & test: config + hidden gate ------------------------------
    def build_bundles(self, sid):
        """Approved bundles the UI offers to build, each tagged with whether it has
        been built (config generated) and whether the hidden gate advanced it."""
        root = self.sessions_root
        approved, _ = self._approved_for_stage4(sid)
        specs_dir = sessions.path(root, sid, sessions.SPECS_DIR)
        out = []
        for b in approved:
            built = os.path.exists(os.path.join(specs_dir, sessions.agent_yaml_name(b["id"], "test")))
            gate = sessions._read_json(os.path.join(specs_dir, sessions.gate_record_name(b["id"]))) or {}
            out.append({"id": b["id"], "name": b.get("name") or b["id"],
                        "built": built, "advanced": bool(gate.get("advanced"))})
        return out

    def _build_model_fn(self):
        """A Sonnet caller from the saved key, for the interview + config engines.
        Raises ``no_api_key`` when no key is saved. The ``anthropic`` client is built
        lazily on first call, so injected stubs that never call it run without the SDK."""
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        holder = {}

        def call(system, user):
            if "fn" not in holder:
                holder["fn"] = agent_configgen._default_model_fn(agent_configgen.DEFAULT_MODEL)
            return holder["fn"](system, user)
        return call

    def _approved_for_stage4(self, sid):
        root = self.sessions_root
        reviewed = sessions.read_json_artifact(root, sid, sessions.REVIEWED) or {}
        event_log = sessions.read_json_artifact(root, sid, sessions.EVENT_LOG) or []
        rep = artifact_review.ReviewReport()
        reviewed = artifact_review.validate_reviewed(
            reviewed, set(artifact_review.event_index(event_log)), rep)
        approved = [b for b in reviewed["bundles"]
                    if b.get("disposition") == "approve_for_build"]
        return approved, event_log

    # --- Phase 2: structured interview (gap detector) --------------------
    def start_interview(self, sid, selected_ids=None):
        root = self.sessions_root
        job = {"state": "running", "error": None, "generated": 0}
        self.interview_jobs[sid] = job

        def run():
            try:
                approved, event_log = self._approved_for_stage4(sid)
                if selected_ids is not None:
                    approved = [b for b in approved if b["id"] in set(selected_ids)]
                model_fn = self._build_model_fn() if approved else None
                for b in approved:
                    questions = self.interview_fn(b, event_log, model_fn=model_fn)["questions"]
                    # preserve any prior answer whose question id still maps
                    prior = sessions.read_json_artifact(
                        root, sid, sessions.interview_filename(b["id"])) or {}
                    qids = {q["id"] for q in questions}
                    answers = [a for a in (prior.get("answers") or [])
                               if a.get("q") in qids and str(a.get("answer", "")).strip()]
                    rec = _interview_record(b["id"], questions, answers)
                    _write_json(sessions.path(root, sid, sessions.interview_filename(b["id"])), rec)
                    job["generated"] += 1
                sessions.update_meta(root, sid, interview_at=sessions._now())
                job.update(state="done")
            except Exception as e:
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    def interview_data(self, sid):
        approved, _ = self._approved_for_stage4(sid)
        out = {}
        for b in approved:
            rec = sessions.read_json_artifact(self.sessions_root, sid,
                                              sessions.interview_filename(b["id"]))
            out[b["id"]] = rec or {"bundle_id": b["id"], "questions": [], "answers": [],
                                   "unresolved": [], "status": "unstarted"}
        return {"approved_ids": [b["id"] for b in approved], "interviews": out}

    def save_interview(self, sid, bid, answers_map):
        """Persist the worker's answers for one bundle. ``answers_map`` is {qid: text};
        blank/missing => skipped. Recomputes unresolved + status server-side."""
        rec = sessions.read_json_artifact(self.sessions_root, sid,
                                          sessions.interview_filename(bid))
        if rec is None:
            return None
        questions = rec.get("questions", [])
        answers = []
        for q in questions:
            text = str((answers_map or {}).get(q["id"], "") or "").strip()
            if text:
                answers.append({"q": q["id"], "question": q.get("question", ""), "answer": text})
        rec = _interview_record(bid, questions, answers)
        _write_json(sessions.path(self.sessions_root, sid, sessions.interview_filename(bid)), rec)
        return rec

    # --- Build & test agents: config (Phase 1) + hidden gate (Phase 3) ---
    def start_build(self, sid, selected_ids=None):
        """For each selected approved bundle: generate the config (Phase 1), then run
        the hidden logic gate (Phase 3) and record its verdict. One 'building…' flow."""
        root = self.sessions_root
        specs_dir = sessions.path(root, sid, sessions.SPECS_DIR)
        job = {"state": "running", "error": None, "generated": 0, "gated": 0,
               "verdicts": {}, "errors": {}}
        self.build_jobs[sid] = job

        def run():
            try:
                approved, event_log = self._approved_for_stage4(sid)
                if selected_ids is not None:
                    approved = [b for b in approved if b["id"] in set(selected_ids)]
                os.makedirs(specs_dir, exist_ok=True)
                model_fn = self._build_model_fn() if approved else None
                for b in approved:
                    bid = b["id"]
                    try:
                        # (1) generate the configuration directly (Phase 1)
                        _, observed, _ = agent_configgen.make_engine_inputs(b, event_log)
                        sdir = sessions.session_dir(root, sid)
                        answers = agent_configgen._load_answers(sdir, bid)
                        unresolved = agent_configgen._load_unresolved(sdir, bid)
                        compiled = self.config_fn(b, observed, answers, model_fn,
                                                  unresolved=unresolved)
                        for mode in ("test", "live"):
                            _write_text(os.path.join(specs_dir, sessions.agent_yaml_name(bid, mode)),
                                        compiled[mode]["yaml"])
                        _write_json(os.path.join(specs_dir, sessions.grounding_name(bid)),
                                    compiled["grounding"])
                        job["generated"] += 1

                        # (2) hidden logic gate: sandbox the TEST variant + grade (Phase 3)
                        criteria = agent_grader.criteria_from_grounding(compiled["grounding"])
                        events_path = os.path.join(specs_dir, sessions.events_name(bid))
                        verdict = self.gate_fn(b, compiled["test"]["dict"], criteria,
                                               event_log, events_path=events_path)
                        _write_json(os.path.join(specs_dir, sessions.gate_record_name(bid)), verdict)
                        job["gated"] += 1
                        job["verdicts"][bid] = bool(verdict.get("advanced"))
                    except Exception as e:
                        job["errors"][bid] = f"{type(e).__name__}: {e}"
                sessions.update_meta(root, sid, build_at=sessions._now())
                job.update(state="done")
            except Exception as e:
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    # --- explicit platform cleanup (C2): never automatic -----------------
    def cleanup_platform(self, sid):
        """Gather the Trace-created ids from this session's gate + run records and
        delete them via the cleanup seam. Returns the removal summary."""
        session_ids, file_ids, agent_ids = [], [], []
        # gate records (Phase 3): the kept hidden-gate sessions
        for g in sessions.list_gate_records(self.sessions_root, sid):
            if g.get("session_id"):
                session_ids.append(g["session_id"])
        # any manual-eval run records (dormant path)
        for r in sessions.list_runs(self.sessions_root, sid):
            for s in r.get("sessions", []):
                if s.get("session_id"):
                    session_ids.append(s["session_id"])
            file_ids.extend(r.get("uploaded_file_ids", []) or [])
            if r.get("agent_id"):
                agent_ids.append(r["agent_id"])
        # de-dup (agents are reused across rounds/bundles)
        session_ids = list(dict.fromkeys(session_ids))
        file_ids = list(dict.fromkeys(file_ids))
        agent_ids = list(dict.fromkeys(agent_ids))
        result = self.cleanup_fn(session_ids, file_ids, agent_ids)
        return {"requested": {"sessions": session_ids, "files": file_ids, "agents": agent_ids},
                **(result or {})}


# --------------------------------------------------------------------------
# Request handling
# --------------------------------------------------------------------------
def make_handler(state):
    root = state.sessions_root

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # --- io helpers ---
        def _json(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _text(self, code, body, ctype):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        # --- routing ---
        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if p == "/" or p == "/index.html":
                return self._serve_static("index.html")
            if p.startswith("/static/"):
                return self._serve_static(p[len("/static/"):])
            if p == "/api/status":
                return self._status()
            if p == "/api/settings":
                return self._json(200, state.keystore.info())
            if p == "/api/permissions":
                return self._json(200, permissions.check_all())
            if p == "/api/sessions":
                return self._json(200, {"sessions": sessions.list_sessions(root)})
            if p == "/api/fixtures":
                return self._json(200, {"fixtures": [
                    {"name": f["name"], "has_answer_key": bool(f["answer_key"])}
                    for f in list_fixtures()]})
            m = re.match(r"^/api/sessions/([^/]+)(/.*)?$", p)
            if m:
                return self._session_get(m.group(1), m.group(2) or "")
            return self._json(404, {"error": "not_found"})

        def do_POST(self):
            p = self.path.split("?", 1)[0]
            body = self._body()
            # save one bundle's interview answers
            mi = re.match(r"^/api/sessions/([^/]+)/interview/([^/]+)$", p)
            if mi:
                return self._save_interview(mi.group(1), mi.group(2), body)
            if p == "/api/settings/key":
                return self._save_key(body)
            if p == "/api/settings/test-key":
                return self._json(200, _test_key(state))
            if p == "/api/capture/start":
                return self._capture_start()
            if p == "/api/capture/stop":
                return self._capture_stop()
            if p == "/api/capture/pause":
                return self._capture_ctl("pause")
            if p == "/api/capture/resume":
                return self._capture_ctl("resume")
            if p == "/api/load-fixture":
                return self._load_fixture(body)
            m = re.match(r"^/api/sessions/([^/]+)/"
                         r"(dry-run|reconstruct|discovery-dry-run|discover|score|"
                         r"reviewed-bundles|generate-specs|specs-dry-run|interview|"
                         r"interview-dry-run|build|"
                         r"cleanup-platform)$", p)
            if m:
                sid, action = m.group(1), m.group(2)
                if action == "dry-run":
                    return self._dry_run(sid, body)
                if action == "reconstruct":
                    return self._reconstruct(sid, body)
                if action == "discovery-dry-run":
                    return self._discovery_dry_run(sid)
                if action == "discover":
                    return self._discover(sid)
                if action == "score":
                    return self._score(sid, body)
                if action == "reviewed-bundles":
                    return self._save_reviewed(sid, body)
                if action == "generate-specs":
                    return self._generate_specs(sid)
                if action == "specs-dry-run":
                    return self._specs_dry_run(sid)
                if action == "interview":
                    return self._interview(sid, body)
                if action == "interview-dry-run":
                    return self._interview_dry_run(sid)
                if action == "cleanup-platform":
                    return self._cleanup_platform(sid)
                return self._build(sid, body)
            return self._json(404, {"error": "not_found"})

        def do_DELETE(self):
            if self.path.split("?", 1)[0] == "/api/settings/key":
                state.keystore.clear_key()
                return self._json(200, {"saved": False})
            return self._json(404, {"error": "not_found"})

        # --- static ---
        def _serve_static(self, rel):
            rel = rel.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
                return self._json(404, {"error": "not_found"})
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as f:
                self._text(200, f.read(), _CONTENT_TYPES.get(ext, "application/octet-stream"))

        # --- settings ---
        def _save_key(self, body):
            key = (body or {}).get("key", "")
            try:
                backend = state.keystore.save_key(key)
            except ValueError:
                return self._json(400, {"error": "empty_key"})
            return self._json(200, {**state.keystore.info(), "backend": backend})

        # --- capture ---
        def _status(self):
            with state.lock:
                if state.controller and state.controller.running:
                    s = state.controller.status()
                    s["session_id"] = state.current_sid
                    s["state"] = "paused" if s["paused"] else "capturing"
                else:
                    s = {"running": False, "state": "idle", "session_id": None}
            return self._json(200, s)

        def _capture_start(self):
            with state.lock:
                if state.controller and state.controller.running:
                    return self._json(409, {"error": "already_running",
                                            "session_id": state.current_sid})
                sid = sessions.create_session(root)
                out = sessions.path(root, sid, sessions.RAW)
                controller = state.controller_factory(out)
                try:
                    controller.start()
                except Exception as e:
                    return self._json(500, {"error": "start_failed", "detail": str(e),
                                            "session_id": sid})
                state.controller = controller
                state.current_sid = sid
                s = controller.status()
                s["session_id"] = sid
                return self._json(200, s)

        def _capture_stop(self):
            with state.lock:
                if not (state.controller and state.controller.running):
                    return self._json(409, {"error": "not_running"})
                sid = state.current_sid
                controller = state.controller
                controller.stop()
                count = sessions.artifacts(root, sid)["raw_event_count"]
                # Completed-session observability + fail-loud signal (Item 3).
                health = controller.summary()
                sessions.update_meta(root, sid, stopped_at=sessions._now(),
                                     raw_event_count=count,
                                     source_summary=health["by_source"],
                                     capture_warning=health["message"])
                state.controller = None
                state.current_sid = None
            return self._json(200, {"session_id": sid, "raw_event_count": count,
                                    "capture_health": health})

        def _capture_ctl(self, action):
            with state.lock:
                if not (state.controller and state.controller.running):
                    return self._json(409, {"error": "not_running"})
                try:
                    getattr(state.controller, action)()
                except RuntimeError as e:
                    return self._json(409, {"error": str(e)})
                s = state.controller.status()
                s["session_id"] = state.current_sid
            return self._json(200, s)

        # --- session reads ---
        def _session_get(self, sid, sub):
            if sub in ("", "/"):
                meta = sessions.read_meta(root, sid)
                meta["artifacts"] = sessions.artifacts(root, sid)
                return self._json(200, meta)
            if sub == "/event_log":
                data = sessions.read_json_artifact(root, sid, sessions.EVENT_LOG)
                return self._json(200 if data is not None else 404,
                                  data if data is not None else {"error": "not_reconstructed"})
            if sub == "/validation":
                data = sessions.read_json_artifact(root, sid, sessions.VALIDATION)
                return self._json(200 if data is not None else 404,
                                  data if data is not None else {"error": "not_reconstructed"})
            if sub == "/raw":
                data = sessions.read_raw_events(root, sid)
                return self._json(200 if data is not None else 404,
                                  data if data is not None else {"error": "no_raw"})
            if sub == "/reconstruct/status":
                job = state.recon_jobs.get(sid)
                return self._json(200, job or {"state": "idle"})
            if sub == "/discovery":
                data = sessions.read_json_artifact(root, sid, sessions.DISCOVERY)
                return self._json(200 if data is not None else 404,
                                  data if data is not None else {"error": "not_discovered"})
            if sub == "/scorecard":
                data = sessions.read_json_artifact(root, sid, sessions.SCORECARD)
                return self._json(200 if data is not None else 404,
                                  data if data is not None else {"error": "not_scored"})
            if sub == "/discover/status":
                job = state.discover_jobs.get(sid)
                return self._json(200, job or {"state": "idle"})
            if sub == "/review-data":
                return self._review_data(sid)
            if sub == "/reviewed-bundles":
                data = sessions.read_json_artifact(root, sid, sessions.REVIEWED)
                return self._json(200 if data is not None else 404,
                                  data if data is not None else {"error": "not_reviewed"})
            if sub == "/specs":
                return self._json(200, {"specs": sessions.list_specs(root, sid)})
            if sub == "/generate-specs/status":
                job = state.spec_jobs.get(sid)
                return self._json(200, job or {"state": "idle"})
            if sub == "/agents":
                return self._json(200, {"agents": sessions.list_agents(root, sid)})
            if sub == "/build-bundles":
                return self._json(200, {"bundles": state.build_bundles(sid)})
            if sub == "/build/status":
                return self._json(200, state.build_jobs.get(sid) or {"state": "idle"})
            if sub == "/interview-data":
                return self._json(200, state.interview_data(sid))
            if sub == "/interview/status":
                return self._json(200, state.interview_jobs.get(sid) or {"state": "idle"})
            return self._json(404, {"error": "not_found"})

        # --- reconstruction ---
        def _dry_run(self, sid, body):
            raw_path = sessions.path(root, sid, sessions.RAW)
            try:
                raw = chunking.load_raw(raw_path)
            except FileNotFoundError:
                return self._json(404, {"error": "no_raw"})
            cs = int((body or {}).get("chunk_size", 120))
            ov = int((body or {}).get("overlap", 20))
            chunks = chunking.chunk(raw, chunk_size=cs, overlap=ov)
            return self._json(200, {
                "event_count": len(raw),
                "system_prompt": SYSTEM_PROMPT,
                "chunk_count": len(chunks),
                "chunks": [
                    {"index": i + 1, "total": len(chunks),
                     "lines": [chunking.render_event(ev) for ev in ch]}
                    for i, ch in enumerate(chunks)
                ],
            })

        def _reconstruct(self, sid, body):
            count = sessions.artifacts(root, sid)["raw_event_count"]
            if count == 0:
                return self._json(400, {"error": "empty_recording"})
            if not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            if count < SHORT_RECORDING_THRESHOLD and not (body or {}).get("force"):
                return self._json(409, {"warning": "short_recording",
                                        "event_count": count,
                                        "detail": "Few events captured. Reconstructing "
                                                  "will call the model and cost money. "
                                                  "Resend with force=true to proceed."})
            with state.lock:
                existing = state.recon_jobs.get(sid)
                if existing and existing.get("state") == "running":
                    return self._json(409, {"error": "already_running"})
                state.start_reconstruction(sid)
            return self._json(202, {"state": "running", "session_id": sid})

        # --- discovery (Stage 2) ---
        def _load_fixture(self, body):
            name = (body or {}).get("name", "")
            fx = next((f for f in list_fixtures() if f["name"] == name), None)
            if not fx:
                return self._json(404, {"error": "unknown_fixture"})
            sid = sessions.create_session(root, label=f"fixture:{name}")
            with open(fx["event_log"], "r", encoding="utf-8") as src, \
                    open(sessions.path(root, sid, sessions.EVENT_LOG), "w", encoding="utf-8") as dst:
                dst.write(src.read())
            try:
                with open(sessions.path(root, sid, sessions.EVENT_LOG)) as f:
                    n = len(json.load(f))
            except (OSError, json.JSONDecodeError):
                n = None
            sessions.update_meta(root, sid, fixture=name,
                                 answer_key_path=fx["answer_key"], event_count=n)
            return self._json(200, {"session_id": sid, "fixture": name,
                                    "has_answer_key": bool(fx["answer_key"]),
                                    "event_count": n})

        def _discovery_dry_run(self, sid):
            log_path = sessions.path(root, sid, sessions.EVENT_LOG)
            try:
                events = load_event_log(log_path)
            except FileNotFoundError:
                return self._json(404, {"error": "no_event_log"})
            return self._json(200, {
                "event_count": len(events),
                "system_prompt": DISCOVERY_SYSTEM_PROMPT,
                "rendered_log": render_event_log(events),
            })

        def _discover(self, sid):
            if not sessions.artifacts(root, sid)["event_log"]:
                return self._json(400, {"error": "no_event_log"})
            if not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            with state.lock:
                existing = state.discover_jobs.get(sid)
                if existing and existing.get("state") == "running":
                    return self._json(409, {"error": "already_running"})
                state.start_discovery(sid)
            return self._json(202, {"state": "running", "session_id": sid})

        def _score(self, sid, body):
            discovery = sessions.read_json_artifact(root, sid, sessions.DISCOVERY)
            if discovery is None:
                return self._json(400, {"error": "not_discovered"})
            meta = sessions.read_meta(root, sid)
            ak_path = (body or {}).get("answer_key_path") or meta.get("answer_key_path")
            if not ak_path or not os.path.exists(ak_path):
                return self._json(400, {"error": "no_answer_key"})
            if not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            os.environ["ANTHROPIC_API_KEY"] = state.keystore.get_key()
            try:
                answer_key = discovery_scorer.load_answer_key(ak_path)
                sc = state.score_fn(discovery, answer_key)
            except Exception as e:
                return self._json(500, {"error": "score_failed", "detail": str(e)})
            with open(sessions.path(root, sid, sessions.SCORECARD), "w", encoding="utf-8") as f:
                json.dump(sc, f, indent=2, ensure_ascii=False)
            sessions.update_meta(root, sid, scored_at=sessions._now())
            return self._json(200, sc)

        # --- review step (Stage 3) ---
        def _review_data(self, sid):
            """Seed the review surface: the editable bundles/anti-bundles plus the
            verbatim observed examples (by event id) for the UI to render."""
            event_log = sessions.read_json_artifact(root, sid, sessions.EVENT_LOG)
            if event_log is None:
                return self._json(404, {"error": "no_event_log"})
            # resume prior edits if a reviewed file exists; else seed from discovery
            reviewed = sessions.read_json_artifact(root, sid, sessions.REVIEWED)
            if reviewed is None:
                discovery = sessions.read_json_artifact(root, sid, sessions.DISCOVERY)
                if discovery is None:
                    return self._json(404, {"error": "not_discovered"})
                reviewed = artifact_review.seed_review(discovery)
            idx = artifact_review.event_index(event_log)
            ids = set()
            for item in reviewed.get("bundles", []) + reviewed.get("anti_bundles", []):
                ids.update(item.get("evidence_event_ids", []))
                ids.update(item.get("included_example_event_ids", []))
            examples = {i: {k: idx[i].get(k) for k in artifact_review._EXAMPLE_FIELDS}
                        for i in ids if i in idx}
            return self._json(200, {"reviewed": reviewed, "examples_by_id": examples})

        def _save_reviewed(self, sid, body):
            event_log = sessions.read_json_artifact(root, sid, sessions.EVENT_LOG)
            if event_log is None:
                return self._json(404, {"error": "no_event_log"})
            report = artifact_review.ReviewReport()
            cleaned = artifact_review.validate_reviewed(
                body or {}, set(artifact_review.event_index(event_log)), report)
            with open(sessions.path(root, sid, sessions.REVIEWED), "w", encoding="utf-8") as f:
                json.dump(cleaned, f, indent=2, ensure_ascii=False)
            sessions.update_meta(root, sid, reviewed_at=sessions._now())
            return self._json(200, {"saved": True, "reviewed": cleaned,
                                    "errors": report.errors, "flags": report.flags})

        # --- artifact engine (Stage 3) ---
        def _specs_dry_run(self, sid):
            reviewed = sessions.read_json_artifact(root, sid, sessions.REVIEWED)
            event_log = sessions.read_json_artifact(root, sid, sessions.EVENT_LOG)
            if reviewed is None:
                return self._json(400, {"error": "not_reviewed"})
            report = artifact_review.ReviewReport()
            reviewed = artifact_review.validate_reviewed(
                reviewed, set(artifact_review.event_index(event_log or [])), report)
            approved = [b for b in reviewed["bundles"]
                        if b.get("disposition") == "approve_for_build"]
            return self._json(200, {
                "system_prompt": ARTIFACT_SYSTEM_PROMPT,
                "approved_count": len(approved),
                "bundles": [{"id": b["id"], "material": build_prompt_material(
                    b, artifact_review.fetch_examples(event_log or [], b["included_example_event_ids"]))}
                    for b in approved],
            })

        def _generate_specs(self, sid):
            if not sessions.artifacts(root, sid)["reviewed"]:
                return self._json(400, {"error": "not_reviewed"})
            reviewed = sessions.read_json_artifact(root, sid, sessions.REVIEWED) or {}
            approved = sum(1 for b in (reviewed.get("bundles") or [])
                           if b.get("disposition") == "approve_for_build")
            if approved and not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            with state.lock:
                existing = state.spec_jobs.get(sid)
                if existing and existing.get("state") == "running":
                    return self._json(409, {"error": "already_running"})
                state.start_spec_generation(sid)
            return self._json(202, {"state": "running", "session_id": sid})

        # --- Build & test agents: config (Phase 1) + hidden gate (Phase 3) ---
        def _build(self, sid, body=None):
            if not sessions.artifacts(root, sid)["reviewed"]:
                return self._json(400, {"error": "not_reviewed"})
            approved, _ = state._approved_for_stage4(sid)
            if approved and not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            selected = (body or {}).get("bundle_ids")    # None => all approved
            with state.lock:
                existing = state.build_jobs.get(sid)
                if existing and existing.get("state") == "running":
                    return self._json(409, {"error": "already_running"})
                state.start_build(sid, selected_ids=selected)
            return self._json(202, {"state": "running", "session_id": sid})

        def _cleanup_platform(self, sid):
            try:
                result = state.cleanup_platform(sid)
            except Exception as e:
                return self._json(200, {"error": f"{type(e).__name__}: {e}"})
            return self._json(200, result)

        # --- Phase 2: structured interview ---
        def _interview(self, sid, body=None):
            if not sessions.artifacts(root, sid)["reviewed"]:
                return self._json(400, {"error": "not_reviewed"})
            approved, _ = state._approved_for_stage4(sid)
            if approved and not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            selected = (body or {}).get("bundle_ids")
            with state.lock:
                existing = state.interview_jobs.get(sid)
                if existing and existing.get("state") == "running":
                    return self._json(409, {"error": "already_running"})
                state.start_interview(sid, selected_ids=selected)
            return self._json(202, {"state": "running", "session_id": sid})

        def _interview_dry_run(self, sid):
            approved, event_log = state._approved_for_stage4(sid)
            return self._json(200, {
                "system_prompt": agent_interview.SYSTEM_PROMPT,
                "bundles": [{"id": b["id"], "material": agent_interview.build_prompt_material(
                    *agent_configgen.make_engine_inputs(b, event_log)[:2])} for b in approved],
            })

        def _save_interview(self, sid, bid, body):
            rec = state.save_interview(sid, bid, (body or {}).get("answers") or {})
            if rec is None:
                return self._json(404, {"error": "no_interview"})
            return self._json(200, rec)

    return Handler


def _test_key(state):
    """Make one tiny, cheap call to confirm the saved key works (handoff §4.1)."""
    key = state.keystore.get_key()
    if not key:
        return {"valid": False, "detail": "No API key saved. Add one in Settings."}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(model="claude-haiku-4-5", max_tokens=1,
                               messages=[{"role": "user", "content": "ping"}])
        return {"valid": True, "detail": "Key accepted by Anthropic."}
    except Exception as e:
        # never echo the key; surface the error type/message only
        return {"valid": False, "detail": f"{type(e).__name__}: {e}"}


def build_server(host="127.0.0.1", port=8765, sessions_root="captures/sessions",
                 collector_port=8137, state=None):
    state = state or AppState(sessions_root, collector_port=collector_port)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    return httpd, state


def main():
    ap = argparse.ArgumentParser(description="Trace Engine 1 — local test UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--sessions-root", default="captures/sessions")
    ap.add_argument("--collector-port", type=int, default=8137)
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    httpd, _ = build_server(port=args.port, sessions_root=args.sessions_root,
                            collector_port=args.collector_port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Trace test UI: {url}")
    print(f"  sessions  -> {os.path.abspath(args.sessions_root)}")
    print(f"  collector -> 127.0.0.1:{args.collector_port} (browser extension target)")
    print("Ctrl-C to stop.")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
