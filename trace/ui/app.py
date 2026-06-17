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
import calendar
import json
import os
import re
import shutil
import ssl
import threading
import time
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
from ..agent import connect as agent_connect
from ..agent import config as agent_config
from ..agent import deploy as agent_deploy
from . import cloud, db, keystore, permissions, sessions, users


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

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")          # founder Test UI
APP_STATIC_DIR = os.path.join(os.path.dirname(__file__), "app_static")  # end-user UI (WS2)
DISCOVERY_FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "discovery", "fixtures")
SHORT_RECORDING_THRESHOLD = 5  # events; below this, warn before a paid run

# Pipeline POST actions the end-user surface may drive (WS3 allow-list; reuses the exact
# same handler methods as the Test UI — no forked pipeline logic).
_USER_SESSION_ACTIONS = frozenset((
    "dry-run", "reconstruct", "discovery-dry-run", "discover", "reviewed-bundles",
    "interview", "interview-dry-run", "build", "connection-identity", "deploy",
    "cleanup-platform"))


def _signup_enabled():
    """Self-registration is on by default; an operator can disable it with
    TRACE_DISABLE_SIGNUP=1 to run provisioned-only."""
    return os.environ.get("TRACE_DISABLE_SIGNUP", "").strip().lower() not in ("1", "true", "yes")


def _token_stale(expires_at_iso, margin=None):
    """True if a stored Pipedream-token expiry is missing, unparseable, past, or within
    the safety margin — i.e. the credential should be refreshed before a run (WS5)."""
    margin = agent_config.TOKEN_REFRESH_MARGIN_SECONDS if margin is None else margin
    if not expires_at_iso:
        return True
    try:
        exp = calendar.timegm(time.strptime(expires_at_iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return True
    return time.time() >= exp - margin


def list_fixtures():
    """Discover bundled validation fixtures: *_event_log.json files, each with an
    optional matching *_answer_key.json for offline validation."""
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


def _apply_pipedream_settings(key_store):
    """Push stored Pipedream Connect settings into ``agent_config`` so every consumer
    (ConnectClient defaults, ``config.pipedream_mcp_url`` at deploy, preflight) sees them
    without environment variables. No-op if the store doesn't support them (e.g. a test
    stub) or none are saved."""
    getpd = getattr(key_store, "get_pipedream", None)
    if not callable(getpd):
        return
    try:
        pd = getpd() or {}
    except Exception:
        return
    if pd.get("project_id"):
        agent_config.PIPEDREAM_PROJECT = pd["project_id"]
    if pd.get("client_id"):
        agent_config.PIPEDREAM_CLIENT_ID = pd["client_id"]
    if pd.get("client_secret"):
        agent_config.PIPEDREAM_CLIENT_SECRET = pd["client_secret"]
    if pd.get("environment"):
        agent_config.PIPEDREAM_ENVIRONMENT = pd["environment"]


class AppState:
    def __init__(self, sessions_root, collector_port=8137,
                 reconstruct_fn=None, controller_factory=None, key_store=None,
                 discovery_fn=None, score_fn=None, artifact_fn=None,
                 interview_fn=None, config_fn=None, cleanup_fn=None,
                 connect_fn=None, deploy_fn=None, cloud_backend=None, proxy_fn=None):
        self.sessions_root = sessions_root
        self.collector_port = collector_port
        # Phase 2 (desktop app): when a cloud backend URL is configured, this
        # process is the local "desktop node" — it runs capture locally and
        # proxies auth + pipeline to the cloud. Unset (env or arg) → today's exact
        # behavior (local dev, and the cloud deployment itself). Switched only by
        # configuration, so dual-run stays first-class.
        self.cloud_backend = (
            cloud_backend or os.environ.get("TRACE_CLOUD_BACKEND") or "").strip().rstrip("/") or None
        self.proxy_fn = proxy_fn or cloud.http_proxy   # injectable for tests
        self.local_to_cloud_sid = {}                   # desktop: local capture sid -> cloud sid
        self.reconstruct_fn = reconstruct_fn or _real_reconstruct
        self.discovery_fn = discovery_fn or _real_discover
        self.score_fn = score_fn or discovery_scorer.score
        self.artifact_fn = artifact_fn or _real_generate_specs
        # Phase 2: the interview (gap detector) + the Phase-1 config engine. All
        # injectable so the build chain runs offline against stubs.
        self.interview_fn = interview_fn or agent_interview.generate_questions
        self.config_fn = config_fn or agent_configgen.compile_config
        self.cleanup_fn = cleanup_fn or self._default_cleanup
        # Phase 4: the connection layer. ``connect_fn`` returns a ConnectClient object
        # (orchestration calls several methods on it); tests inject a stub.
        self.connect_fn = connect_fn or self._default_connect_client
        # Phase 6: live deployment. ``deploy_fn`` deploys one bundle's LIVE agent over the
        # bound connection and returns its live record; tests inject a stub.
        self.deploy_fn = deploy_fn or self._default_deploy
        self.controller_factory = controller_factory or (
            lambda out: CaptureController(out, collector_port=collector_port))
        self.keystore = key_store or keystore
        self.controller = None       # current CaptureController (one session at a time)
        self.current_sid = None
        # Load any stored Pipedream Connect settings into the agent config so every
        # consumer (ConnectClient defaults, config.pipedream_mcp_url at deploy, preflight)
        # sees them without env vars.
        _apply_pipedream_settings(self.keystore)
        self.recon_jobs = {}         # sid -> {state, current, total, error, flags, errors}
        self.discover_jobs = {}      # sid -> {state, error, bundles, anti_bundles}
        self.spec_jobs = {}          # sid -> {state, error, built, notes}
        self.interview_jobs = {}     # sid -> {state, error, generated}
        self.build_jobs = {}         # sid -> {state, error, generated, errors}
        self.connection_jobs = {}    # sid -> {state, error, slug, connect_url, verified, flag}
        self.deploy_jobs = {}        # sid -> {state, error, deployed, errors}
        self.lock = threading.Lock()
        os.makedirs(sessions_root, exist_ok=True)

    def _default_cleanup(self, session_ids, file_ids, agent_ids, agent_names=None):
        """Real, human-initiated platform cleanup: requires a key; builds the client and
        deletes the Trace-created ids it is given, plus any agents the platform still has
        under this session's workflow names (``agent_names`` — sweeps orphaned/stale ones)."""
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        from ..agent.api_client import ManagedAgentsClient
        client = ManagedAgentsClient(api_key=key)
        return client.cleanup(session_ids=session_ids, file_ids=file_ids,
                              agent_ids=agent_ids, agent_names=agent_names)

    def _default_connect_client(self):
        """Real connection client (Pipedream Connect + MA vault). Requires a key;
        every platform shape inside is flagged confirm-against-docs."""
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        from ..agent.connect_client import ConnectClient
        return ConnectClient(api_key=key)

    def _default_deploy(self, bundle, live_dict, connection_record, start):
        """Real live deploy for one bundle: creates the LIVE agent over the bound
        connection (vault injected) and starts it on its trigger/schedule. Requires a key."""
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        from ..agent.api_client import ManagedAgentsClient
        client = ManagedAgentsClient(api_key=key)
        return agent_deploy.deploy_live(bundle, live_dict, connection_record, client,
                                        trigger=start.get("trigger"), schedule=start.get("schedule"))

    def _live_client(self):
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        from ..agent.api_client import ManagedAgentsClient
        return ManagedAgentsClient(api_key=key)

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

    # --- Build agents: config ------------------------------
    def build_bundles(self, sid):
        """Approved bundles the UI offers to build, each tagged with whether it has
        been built (config generated)."""
        root = self.sessions_root
        approved, _ = self._approved_for_stage4(sid)
        specs_dir = sessions.path(root, sid, sessions.SPECS_DIR)
        out = []
        for b in approved:
            built = os.path.exists(os.path.join(specs_dir, sessions.agent_yaml_name(b["id"], "live")))
            out.append({"id": b["id"], "name": b.get("name") or b["id"], "built": built})
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

    # --- Build agents: config (Phase 1) ---
    def start_build(self, sid, selected_ids=None):
        """For each selected approved bundle: generate the config (Phase 1). One
        'building…' flow."""
        root = self.sessions_root
        specs_dir = sessions.path(root, sid, sessions.SPECS_DIR)
        job = {"state": "running", "error": None, "generated": 0, "errors": {}}
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
                        _write_text(os.path.join(specs_dir, sessions.agent_yaml_name(bid, "live")),
                                    compiled["live"]["yaml"])
                        _write_json(os.path.join(specs_dir, sessions.grounding_name(bid)),
                                    compiled["grounding"])
                        job["generated"] += 1
                    except Exception as e:
                        job["errors"][bid] = f"{type(e).__name__}: {e}"
                sessions.update_meta(root, sid, build_at=sessions._now())
                job.update(state="done")
            except Exception as e:
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    # --- explicit platform cleanup (C2): never automatic -----------------
    def cleanup_platform(self, sid):
        """Gather the Trace-created ids from this session's run + deploy records and
        delete them via the cleanup seam, plus sweep any agents still on the platform
        under this session's workflow names (orphaned/stale duplicates from earlier
        rounds — e.g. test-era agents). Returns the removal summary."""
        session_ids, file_ids, agent_ids = [], [], []
        # the agent names Trace uses for this session's bundles (matches configgen's
        # _envelope: name -> bundle name/id), so the sweep can reach stale duplicates.
        approved, _ = self._approved_for_stage4(sid)
        agent_names = list(dict.fromkeys(
            str(b.get("name") or b.get("id") or "Agent").strip() for b in approved))
        # live deploy records (Phase 6): the live sessions + agents
        for d in sessions.list_deploy_records(self.sessions_root, sid):
            if d.get("session_id"):
                session_ids.append(d["session_id"])
            if d.get("agent_id"):
                agent_ids.append(d["agent_id"])
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
        result = self.cleanup_fn(session_ids, file_ids, agent_ids, agent_names=agent_names)
        return {"requested": {"sessions": session_ids, "files": file_ids,
                              "agents": agent_ids, "agent_names": agent_names},
                **(result or {})}

    # --- Phase 4: connection layer (Pipedream Connect + MA vault) ---------
    def _external_user_id(self, sid):
        """The Pipedream/vault isolation key for a session. End-user sessions share one
        per-customer id (``trace-user-<id>`` -> one vault per user, WS3); the founder's
        Test UI keeps a per-session id (unchanged)."""
        owner = (sessions.read_meta(self.sessions_root, sid) or {}).get("owner_user_id")
        return users.external_user_id(owner) if owner else f"trace-{sid}"

    def _load_connection(self, sid):
        """Load the per-session connection record, or seed one with the session's
        external_user_id and the current app union."""
        root = self.sessions_root
        rec = sessions.read_json_artifact(root, sid, sessions.CONNECTION)
        approved, event_log = self._approved_for_stage4(sid)
        union, unmapped = agent_connect.app_union(approved, event_log)
        if rec is None:
            rec = agent_connect.new_record(self._external_user_id(sid), union)
        rec = agent_connect.merge_union(rec, union, unmapped)
        return rec, union

    def _save_connection(self, sid, rec):
        _write_json(sessions.path(self.sessions_root, sid, sessions.CONNECTION), rec)

    def connection_data(self, sid):
        rec, union = self._load_connection(sid)
        self._save_connection(sid, rec)
        return {"record": rec, "union": union, "unmapped": rec.get("unmapped", []),
                "expectation": agent_config.CONNECT_EXPECTATION_COPY}

    def set_expected_identity(self, sid, email):
        rec, _ = self._load_connection(sid)
        rec["expected_identity"] = str(email or "").strip()
        self._save_connection(sid, rec)
        return rec

    def start_connect(self, sid, slug):
        """Open the (simulated in dev) Connect flow for one app; the job carries the
        connect_url the worker would open."""
        job = {"state": "running", "error": None, "slug": slug, "connect_url": None}
        self.connection_jobs[sid] = job

        def run():
            try:
                rec, _ = self._load_connection(sid)
                client = self.connect_fn()
                tok = agent_connect.connect_app(client, rec, slug)
                self._save_connection(sid, rec)
                job.update(state="done", connect_url=tok.get("connect_url"))
            except Exception as e:
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    def start_connect_callback(self, sid, slug):
        """After the worker authorizes in Google's window and taps Verify: ensure+bind the
        vault, fetch the account, and verify identity. If no account exists yet (not
        authorized), it comes back unconnected; mismatches are flagged, never used silently."""
        job = {"state": "running", "error": None, "slug": slug}
        self.connection_jobs[sid] = job

        def run():
            try:
                rec, _ = self._load_connection(sid)
                client = self.connect_fn()
                agent_connect.bind_and_verify(client, rec, slug, rec.get("expected_identity", ""))
                self._save_connection(sid, rec)
                entry = rec["apps"].get(slug, {})
                job.update(state="done", verified=entry.get("verified"),
                           connected=entry.get("connected"),
                           usable=entry.get("usable"), flag=entry.get("flag"))
            except Exception as e:
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    # --- Phase 6: live deployment ----------------------------------------
    def _refresh_stale_bindings(self, sid, conn):
        """WS5: re-mint + rotate the Pipedream token for any usable app whose stored token
        is stale, so a run long after Connect still authenticates — no manual re-verify.
        Best-effort: a refresh failure never blocks deploy (a truly dead token would
        surface as the run's own auth error). Reuses ConnectClient.refresh_binding, which
        rotates only the token and never re-sends the immutable mcp_server_url."""
        vault_ref = (conn or {}).get("vault_ref")
        if not vault_ref:
            return conn
        uid = conn.get("external_user_id")
        stale = [slug for slug, a in (conn.get("apps") or {}).items()
                 if a.get("usable") and _token_stale(a.get("token_expires_at"))]
        if not stale:
            return conn
        try:
            client = self.connect_fn()
        except Exception:
            return conn
        changed = False
        for slug in stale:
            try:
                out = client.refresh_binding(vault_ref, uid, slug)
                conn["apps"][slug]["token_expires_at"] = out.get("expires_at")
                changed = True
            except Exception:
                pass
        if changed:
            self._save_connection(sid, conn)
        return conn

    def _deploy_eligible(self, sid):
        """Approved bundles that have a built config AND a usable connection."""
        root = self.sessions_root
        approved, event_log = self._approved_for_stage4(sid)
        specs_dir = sessions.path(root, sid, sessions.SPECS_DIR)
        conn = sessions.read_json_artifact(root, sid, sessions.CONNECTION) or {}
        any_usable = any(a.get("usable") for a in conn.get("apps", {}).values())
        out = []
        for b in approved:
            built = os.path.exists(os.path.join(specs_dir, sessions.agent_yaml_name(b["id"], "live")))
            if built and any_usable:
                out.append(b)
        return out, event_log, conn

    def deploy_bundles(self, sid):
        eligible, _, _ = self._deploy_eligible(sid)
        specs_dir = sessions.path(self.sessions_root, sid, sessions.SPECS_DIR)
        out = []
        for b in eligible:
            dep = sessions._read_json(os.path.join(specs_dir, sessions.deploy_record_name(b["id"]))) or {}
            out.append({"id": b["id"], "name": b.get("name") or b["id"],
                        "status": dep.get("status"), "start": dep.get("start"),
                        "held_actions": dep.get("held_actions", [])})
        return out

    def start_deploy(self, sid, selected_ids=None, start=None):
        root = self.sessions_root
        specs_dir = sessions.path(root, sid, sessions.SPECS_DIR)
        start = start or {}
        job = {"state": "running", "error": None, "deployed": 0, "errors": {}}
        self.deploy_jobs[sid] = job

        def run():
            try:
                eligible, _, conn = self._deploy_eligible(sid)
                conn = self._refresh_stale_bindings(sid, conn)   # WS5: token fresh before run
                if selected_ids is not None:
                    eligible = [b for b in eligible if b["id"] in set(selected_ids)]
                for b in eligible:
                    bid = b["id"]
                    try:
                        with open(os.path.join(specs_dir, sessions.agent_yaml_name(bid, "live")),
                                  encoding="utf-8") as f:
                            live_dict = yaml.safe_load(f.read())
                        rec = self.deploy_fn(b, live_dict, conn, start)
                        _write_json(os.path.join(specs_dir, sessions.deploy_record_name(bid)), rec)
                        job["deployed"] += 1
                    except Exception as e:
                        job["errors"][bid] = f"{type(e).__name__}: {e}"
                sessions.update_meta(root, sid, deploy_at=sessions._now())
                job.update(state="done")
            except Exception as e:
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    def deploy_resolve(self, sid, bid, action_id, decision):
        """One-tap approve/reject of a held outbound action. Updates the live record and
        best-effort commits/cancels on the platform."""
        specs_dir = sessions.path(self.sessions_root, sid, sessions.SPECS_DIR)
        p = os.path.join(specs_dir, sessions.deploy_record_name(bid))
        rec = sessions._read_json(p)
        if rec is None:
            return {"error": "no_live_record"}
        new_status = "approved" if decision == "approve" else "rejected"
        for a in rec.get("held_actions", []):
            if a.get("action_id") == action_id:
                a["status"] = new_status
        _write_json(p, rec)
        try:                                              # best-effort platform commit
            client = self._live_client()
            (agent_deploy.approve if decision == "approve" else agent_deploy.reject)(
                client, rec.get("session_id"), action_id)
        except Exception:
            pass
        return {"action_id": action_id, "status": new_status}


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

        # --- auth (WS3): cookie <-> end-user; the Test UI never uses any of this ---
        def _cookie(self, name):
            for part in (self.headers.get("Cookie", "") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return None

        def _current_user(self):
            uid = users.session_user(self._cookie(users.COOKIE_NAME))
            return users.get(uid) if uid else None

        # --- desktop mode (Phase 2): proxy everything to the cloud except the few
        # things that must run on this machine (capture + the local-machine probes) ---
        def _is_local_desktop_path(self, p):
            """Paths the desktop node MUST serve itself (never proxy): capture control
            (capture watches THIS machine) and the local-machine probes. Everything
            else — UI assets, auth, all pipeline actions — is proxied to the cloud."""
            if p == "/health" or p in ("/api/status", "/api/permissions"):
                return True
            return p.startswith("/api/u/capture/") or p.startswith("/api/capture/")

        def _should_proxy(self, p):
            return bool(state.cloud_backend) and not self._is_local_desktop_path(p)

        def _proxy(self):
            """Relay the current request to the cloud backend, forwarding the auth
            cookie up and the cloud's status/body/Set-Cookie back down. One localhost
            origin in, one cloud origin out — so the UI never sees cross-origin."""
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
            fwd = {}
            for h in ("Cookie", "Content-Type", "Accept"):
                if self.headers.get(h):
                    fwd[h] = self.headers.get(h)
            try:
                status, headers, data = state.proxy_fn(
                    state.cloud_backend, self.command, self.path, fwd, body)
            except Exception as e:                      # cloud unreachable / DNS / TLS
                return self._json(502, {"error": "cloud_unreachable", "detail": str(e)})
            self.send_response(status)
            for k, v in headers:                        # Content-Type + Set-Cookie preserved
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _upload_raw_to_cloud(self, local_sid):
            """Desktop: POST the just-captured raw_events.jsonl to the cloud, which
            creates a cloud-owned session from it. Returns the cloud session id (or
            None on failure). Forwards the auth cookie so the cloud scopes ownership
            to the logged-in user. JSONL is wrapped in JSON so the existing body
            parsing on the cloud side handles it."""
            raw_path = sessions.path(root, local_sid, sessions.RAW)
            try:
                with open(raw_path, "r", encoding="utf-8") as f:
                    raw_text = f.read()
            except FileNotFoundError:
                raw_text = ""
            payload = json.dumps({"raw": raw_text}).encode("utf-8")
            fwd = {"Content-Type": "application/json"}
            if self.headers.get("Cookie"):
                fwd["Cookie"] = self.headers.get("Cookie")
            status, _headers, data = state.proxy_fn(
                state.cloud_backend, "POST", "/api/u/sessions/raw-upload", fwd, payload)
            if status != 200:
                return None
            try:
                cloud_sid = json.loads(data).get("session_id")
            except (json.JSONDecodeError, AttributeError):
                return None
            if cloud_sid:
                state.local_to_cloud_sid[local_sid] = cloud_sid
            return cloud_sid

        def _raw_upload(self, user, body):
            """Cloud side: materialize a session from raw events uploaded by a desktop
            client, owned by the authenticated user. The pipeline then runs against it
            unchanged. Reached via the proxied POST /api/u/sessions/raw-upload."""
            raw_text = (body or {}).get("raw")
            if raw_text is None:
                return self._json(400, {"error": "missing_raw"})
            sid, count = sessions.create_upload_session(root, user["user_id"], raw_text)
            return self._json(200, {"session_id": sid, "raw_event_count": count})

        def _json_auth(self, code, body, set_token=None, clear=False):
            """JSON response that may set/clear the httpOnly auth cookie. (Secure flag
            omitted for local http; add it behind TLS in production.)"""
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if set_token is not None:
                self.send_header("Set-Cookie",
                                 f"{users.COOKIE_NAME}={set_token}; Path=/; HttpOnly; SameSite=Lax")
            if clear:
                self.send_header("Set-Cookie",
                                 f"{users.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.end_headers()
            self.wfile.write(data)

        def _auth_signup(self, body):
            if not _signup_enabled():
                return self._json(403, {"error": "signup_disabled"})
            try:
                u = users.create_user((body or {}).get("email"), (body or {}).get("password"))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json_auth(200, {"user": u}, set_token=users.start_session(u["user_id"]))

        def _auth_login(self, body):
            u = users.verify((body or {}).get("email"), (body or {}).get("password"))
            if not u:
                return self._json(401, {"error": "invalid_credentials"})
            return self._json_auth(200, {"user": u}, set_token=users.start_session(u["user_id"]))

        def _auth_logout(self):
            users.end_session(self._cookie(users.COOKIE_NAME))
            return self._json_auth(200, {"ok": True}, clear=True)

        def _owns(self, sid, user):
            return (sessions.read_meta(root, sid) or {}).get("owner_user_id") == user["user_id"]

        # --- end-user namespace (WS3): same pipeline, scoped to the logged-in user ---
        def _user_get(self, rest, user):
            if rest in ("/me", ""):
                return self._json(200, {"user": user, "signup_enabled": _signup_enabled()})
            if rest == "/sessions":
                mine = [s for s in sessions.list_sessions(root)
                        if s.get("owner_user_id") == user["user_id"]]
                return self._json(200, {"sessions": mine})
            if rest == "/fixtures":                          # demo entry without mac capture
                return self._json(200, {"fixtures": [
                    {"name": f["name"]} for f in list_fixtures()]})
            if rest == "/preflight":
                return self._json(200, _preflight(state))
            m = re.match(r"^/sessions/([^/]+)(/.*)?$", rest)
            if m:
                if not self._owns(m.group(1), user):
                    return self._json(404, {"error": "not_found"})
                return self._session_get(m.group(1), m.group(2) or "")
            return self._json(404, {"error": "not_found"})

        def _user_post(self, rest, body, user):
            uid = user["user_id"]
            if rest == "/capture/start":
                return self._capture_start(owner=uid)
            if rest == "/capture/stop":
                return self._capture_stop()
            if rest in ("/capture/pause", "/capture/resume"):
                return self._capture_ctl(rest.rsplit("/", 1)[1])
            if rest == "/sessions/raw-upload":           # Phase 2: desktop capture upload
                return self._raw_upload(user, body)
            if rest == "/load-fixture":
                return self._load_fixture(body, owner=uid)
            m = re.match(r"^/sessions/([^/]+)/(.+)$", rest)
            if m:
                sid, sub = m.group(1), m.group(2)
                if not self._owns(sid, user):
                    return self._json(404, {"error": "not_found"})
                mi = re.match(r"^interview/([^/]+)$", sub)
                if mi:
                    return self._save_interview(sid, mi.group(1), body)
                mcb = re.match(r"^connect/([^/]+)/callback$", sub)
                if mcb:
                    return self._connect_callback(sid, mcb.group(1))
                mc = re.match(r"^connect/([^/]+)$", sub)
                if mc:
                    return self._connect_start(sid, mc.group(1))
                mdr = re.match(r"^deploy/([^/]+)/(approve|reject)$", sub)
                if mdr:
                    return self._deploy_resolve(sid, mdr.group(1), mdr.group(2), body)
                if sub in _USER_SESSION_ACTIONS:
                    return self._session_post(sid, sub, body)
            return self._json(404, {"error": "not_found"})

        def _session_post(self, sid, action, body):
            """Dispatch one session-scoped pipeline action — shared by the Test UI and the
            scoped end-user namespace (identical pipeline logic, no fork)."""
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
            if action == "connection-identity":
                return self._set_identity(sid, body)
            if action == "deploy":
                return self._deploy(sid, body)
            return self._build(sid, body)

        # --- routing ---
        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if self._should_proxy(p):                       # Phase 2: desktop → cloud
                return self._proxy()
            if p == "/health":                              # Phase 1: liveness probe
                # Dependency-free on purpose: Render's health check and the
                # human's first smoke test must pass even before keys/DB are set.
                return self._json(200, {"status": "ok", "service": "trace-backend",
                                        "database": "configured" if db.is_configured()
                                        else "local"})
            if p == "/" or p == "/index.html":
                return self._serve_static("index.html")
            if p.startswith("/static/"):
                return self._serve_static(p[len("/static/"):])
            if p == "/app" or p == "/app/":                 # end-user UI (WS2)
                return self._serve_app("index.html")
            if p.startswith("/app/"):
                return self._serve_app(p[len("/app/"):])
            if p == "/api/auth/me":                         # WS3
                return self._json(200, {"user": self._current_user(),
                                        "signup_enabled": _signup_enabled()})
            if p == "/api/u" or p.startswith("/api/u/"):    # scoped end-user API
                u = self._current_user()
                if not u:
                    return self._json(401, {"error": "auth_required"})
                return self._user_get(p[len("/api/u"):], u)
            if p == "/api/status":
                return self._status()
            if p == "/api/settings":
                return self._json(200, state.keystore.info())
            if p == "/api/settings/pipedream":
                pinfo = getattr(state.keystore, "pipedream_info", None)
                return self._json(200, pinfo() if callable(pinfo) else {"configured": False})
            if p == "/api/permissions":
                return self._json(200, permissions.check_all())
            if p == "/api/preflight":
                return self._json(200, _preflight(state))
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
            if self._should_proxy(p):                       # Phase 2: desktop → cloud
                return self._proxy()
            # Desktop mode: capture control runs locally and identity lives in the
            # cloud, so handle these before the /api/u/ local-auth gate (no local
            # user store here). Ownership of the resulting session is established
            # cloud-side at upload time.
            if state.cloud_backend:
                if p in ("/api/u/capture/start", "/api/capture/start"):
                    return self._capture_start(owner=None)
                if p in ("/api/u/capture/stop", "/api/capture/stop"):
                    return self._capture_stop()
                if p in ("/api/u/capture/pause", "/api/capture/pause"):
                    return self._capture_ctl("pause")
                if p in ("/api/u/capture/resume", "/api/capture/resume"):
                    return self._capture_ctl("resume")
            body = self._body()
            if p == "/api/auth/signup":                     # WS3
                return self._auth_signup(body)
            if p == "/api/auth/login":
                return self._auth_login(body)
            if p == "/api/auth/logout":
                return self._auth_logout()
            if p == "/api/u" or p.startswith("/api/u/"):    # scoped end-user API
                u = self._current_user()
                if not u:
                    return self._json(401, {"error": "auth_required"})
                return self._user_post(p[len("/api/u"):], body, u)
            # save one bundle's interview answers
            mi = re.match(r"^/api/sessions/([^/]+)/interview/([^/]+)$", p)
            if mi:
                return self._save_interview(mi.group(1), mi.group(2), body)
            mcb = re.match(r"^/api/sessions/([^/]+)/connect/([^/]+)/callback$", p)
            if mcb:
                return self._connect_callback(mcb.group(1), mcb.group(2))
            mc = re.match(r"^/api/sessions/([^/]+)/connect/([^/]+)$", p)
            if mc:
                return self._connect_start(mc.group(1), mc.group(2))
            mdr = re.match(r"^/api/sessions/([^/]+)/deploy/([^/]+)/(approve|reject)$", p)
            if mdr:
                return self._deploy_resolve(mdr.group(1), mdr.group(2), mdr.group(3), body)
            if p == "/api/settings/key":
                return self._save_key(body)
            if p == "/api/settings/pipedream":
                return self._save_pipedream(body)
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
                         r"interview-dry-run|build|connection-identity|deploy|"
                         r"cleanup-platform)$", p)
            if m:
                return self._session_post(m.group(1), m.group(2), body)
            return self._json(404, {"error": "not_found"})

        def do_DELETE(self):
            dp = self.path.split("?", 1)[0]
            if dp == "/api/settings/key":
                state.keystore.clear_key()
                return self._json(200, {"saved": False})
            if dp == "/api/settings/pipedream":
                clear = getattr(state.keystore, "clear_pipedream", None)
                if callable(clear):
                    clear()
                _apply_pipedream_settings(state.keystore)
                pinfo = getattr(state.keystore, "pipedream_info", None)
                return self._json(200, pinfo() if callable(pinfo) else {"configured": False})
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

        def _serve_app(self, rel):
            """Serve the end-user UI (WS2) from app_static/. Unknown sub-paths fall back to
            index.html so the single-page app can own its own routing."""
            rel = (rel or "").lstrip("/") or "index.html"
            full = os.path.normpath(os.path.join(APP_STATIC_DIR, rel))
            if not full.startswith(APP_STATIC_DIR) or not os.path.isfile(full):
                full = os.path.join(APP_STATIC_DIR, "index.html")
            if not os.path.isfile(full):
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

        def _save_pipedream(self, body):
            """Save Trace's Pipedream Connect settings (project id / client id / secret /
            environment), then push them into the agent config so the live path picks them
            up immediately. The secret is stored securely and never echoed back."""
            save = getattr(state.keystore, "save_pipedream", None)
            if not callable(save):
                return self._json(400, {"error": "unsupported"})
            b = body or {}
            save(project_id=b.get("project_id"), client_id=b.get("client_id"),
                 client_secret=b.get("client_secret"), environment=b.get("environment"))
            _apply_pipedream_settings(state.keystore)
            return self._json(200, state.keystore.pipedream_info())

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

        def _capture_start(self, owner=None):
            with state.lock:
                if state.controller and state.controller.running:
                    return self._json(409, {"error": "already_running",
                                            "session_id": state.current_sid})
                sid = sessions.create_session(root)
                if owner:                                   # WS3: tag the owning end-user
                    sessions.update_meta(root, sid, owner_user_id=owner)
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
            # Desktop mode: the pipeline lives in the cloud, so push the captured
            # stream up and hand the UI back the CLOUD session id — its subsequent
            # (proxied) reconstruct/discover then target the cloud session.
            if state.cloud_backend:
                cloud_sid = self._upload_raw_to_cloud(sid)
                if not cloud_sid:
                    return self._json(502, {"error": "upload_failed",
                                            "local_session_id": sid,
                                            "raw_event_count": count})
                return self._json(200, {"session_id": cloud_sid,
                                        "local_session_id": sid,
                                        "raw_event_count": count,
                                        "capture_health": health})
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
            if sub == "/connection-data":
                return self._json(200, state.connection_data(sid))
            if sub == "/connect/status":
                return self._json(200, state.connection_jobs.get(sid) or {"state": "idle"})
            if sub == "/deploy-bundles":
                return self._json(200, {"bundles": state.deploy_bundles(sid)})
            if sub == "/deploy/status":
                return self._json(200, state.deploy_jobs.get(sid) or {"state": "idle"})
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
        def _load_fixture(self, body, owner=None):
            name = (body or {}).get("name", "")
            fx = next((f for f in list_fixtures() if f["name"] == name), None)
            if not fx:
                return self._json(404, {"error": "unknown_fixture"})
            sid = sessions.create_session(root, label=f"fixture:{name}")
            if owner:                                       # WS3: tag the owning end-user
                sessions.update_meta(root, sid, owner_user_id=owner)
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

        # --- Build agents: config (Phase 1) ---
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

        # --- Phase 4: connection layer ---
        def _set_identity(self, sid, body):
            email = (body or {}).get("email", "")
            if not str(email).strip():
                return self._json(400, {"error": "empty_email"})
            return self._json(200, state.set_expected_identity(sid, email))

        def _connect_start(self, sid, slug):
            if not sessions.artifacts(root, sid)["reviewed"]:
                return self._json(400, {"error": "not_reviewed"})
            if not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            with state.lock:
                existing = state.connection_jobs.get(sid)
                if existing and existing.get("state") == "running":
                    return self._json(409, {"error": "already_running"})
                state.start_connect(sid, slug)
            return self._json(202, {"state": "running", "session_id": sid})

        def _connect_callback(self, sid, slug):
            if not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            with state.lock:
                existing = state.connection_jobs.get(sid)
                if existing and existing.get("state") == "running":
                    return self._json(409, {"error": "already_running"})
                state.start_connect_callback(sid, slug)
            return self._json(202, {"state": "running", "session_id": sid})

        # --- Phase 6: live deployment ---
        def _deploy(self, sid, body=None):
            eligible, _, _ = state._deploy_eligible(sid)
            if not eligible:
                return self._json(400, {"error": "not_eligible"})
            if not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            b = body or {}
            start = {"trigger": b.get("trigger"), "schedule": b.get("schedule")}
            with state.lock:
                existing = state.deploy_jobs.get(sid)
                if existing and existing.get("state") == "running":
                    return self._json(409, {"error": "already_running"})
                state.start_deploy(sid, selected_ids=b.get("bundle_ids"), start=start)
            return self._json(202, {"state": "running", "session_id": sid})

        def _deploy_resolve(self, sid, bid, decision, body):
            action_id = (body or {}).get("action_id")
            if not action_id:
                return self._json(400, {"error": "missing_action_id"})
            return self._json(200, state.deploy_resolve(sid, bid, action_id, decision))

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


def _preflight(state):
    """Confirm credentials/SDK/entitlements BEFORE driving the live pipeline, so the first
    real failure is a diagnosis and not a buried mid-run error. Each check is isolated and
    reports ``{name, ok, detail}`` where ``ok`` is True/False, or None for "not configured".
    Secrets are NEVER echoed — only the error type/message."""
    checks = []
    key = state.keystore.get_key()

    # 1. Anthropic key valid (cheap model call).
    if not key:
        checks.append({"name": "Anthropic API key", "ok": False,
                       "detail": "No API key saved. Add one in Settings."})
    else:
        tk = _test_key(state)
        checks.append({"name": "Anthropic API key", "ok": tk["valid"], "detail": tk["detail"]})

    # 2. Managed Agents reachable + entitled (catches the beta-access gap a messages call misses).
    if not key:
        checks.append({"name": "Managed Agents access", "ok": False, "detail": "Needs an API key."})
    else:
        try:
            from ..agent.api_client import ManagedAgentsClient
            ManagedAgentsClient(api_key=key).ping()
            checks.append({"name": "Managed Agents access", "ok": True,
                           "detail": "Agents API reachable."})
        except Exception as e:
            checks.append({"name": "Managed Agents access", "ok": False,
                           "detail": f"{type(e).__name__}: {e}"})

    # 3/4. Pipedream: configured? then token mints + project reachable.
    configured = all((agent_config.PIPEDREAM_PROJECT, agent_config.PIPEDREAM_CLIENT_ID,
                      agent_config.PIPEDREAM_CLIENT_SECRET))
    if not configured:
        checks.append({"name": "Pipedream Connect", "ok": None,
                       "detail": "Not configured — set PIPEDREAM_PROJECT_ID, "
                                 "PIPEDREAM_CLIENT_ID, PIPEDREAM_CLIENT_SECRET to enable the "
                                 "live path (connect / go live)."})
    else:
        try:
            apps = state.connect_fn().list_apps()
            checks.append({"name": "Pipedream Connect", "ok": True,
                           "detail": f"Token minted; project reachable ({len(apps)} apps)."})
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            if "CERTIFICATE_VERIFY_FAILED" in str(e) or isinstance(e, ssl.SSLCertVerificationError):
                detail += (" — TLS cert verification failed: run `pip install -U certifi` "
                           "(and on macOS the 'Install Certificates.command' for your "
                           "Python), then retry.")
            checks.append({"name": "Pipedream Connect", "ok": False, "detail": detail})

    ok = all(c["ok"] for c in checks if c["ok"] is not None)
    return {"ok": ok, "checks": checks}


def build_server(host="127.0.0.1", port=8765, sessions_root="captures/sessions",
                 collector_port=8137, state=None, cloud_backend=None):
    # Phase 1: ensure the Postgres schema when a database is configured
    # (DATABASE_URL set); a no-op on a local run so boot stays the same path.
    db.init_db()
    state = state or AppState(sessions_root, collector_port=collector_port,
                              cloud_backend=cloud_backend)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    return httpd, state


def main():
    ap = argparse.ArgumentParser(description="Trace Engine 1 — local test UI")
    # Env wins so the same code runs locally and on Render (which injects $PORT),
    # switched only by configuration; the CLI flags remain as explicit overrides
    # and supply the defaults when neither env nor flag is set.
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--sessions-root",
                    default=os.environ.get("TRACE_SESSIONS_ROOT", "captures/sessions"))
    ap.add_argument("--collector-port", type=int, default=8137)
    # Phase 2: when set, this process is the desktop node — capture local, pipeline
    # proxied to this cloud URL. Unset → unchanged local/cloud behavior.
    ap.add_argument("--cloud-backend", default=os.environ.get("TRACE_CLOUD_BACKEND"))
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    httpd, _ = build_server(host=args.host, port=args.port,
                            sessions_root=args.sessions_root,
                            collector_port=args.collector_port,
                            cloud_backend=args.cloud_backend)
    url = f"http://{args.host}:{args.port}/"
    print(f"Trace test UI: {url}")
    print(f"  sessions  -> {os.path.abspath(args.sessions_root)}")
    print(f"  collector -> 127.0.0.1:{args.collector_port} (browser extension target)")
    if args.cloud_backend:
        print(f"  desktop mode -> proxying auth + pipeline to {args.cloud_backend}")
    print("Ctrl-C to stop.")
    # Only pop a browser for a genuine local bind; in the cloud (0.0.0.0 / a
    # non-loopback host) there is no display and the URL isn't reachable here.
    if not args.no_open and args.host in ("127.0.0.1", "localhost"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
