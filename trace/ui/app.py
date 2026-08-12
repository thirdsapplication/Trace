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
import collections
import concurrent.futures
import hmac
import json
import os
import re
import shutil
import ssl
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from ..capture.controller import CaptureController
from ..reconstruct import chunking
from ..reconstruct.prompt import SYSTEM_PROMPT
from ..reconstruct.reconstruct import reconstruct as _real_reconstruct
# The prompt tap needs each engine's model tier + lazy caller. Import the NAMES from
# the engine MODULES directly — NOT `from ..reconstruct import reconstruct`, which the
# package __init__ re-exports as the function (so `.DEFAULT_MODEL` would AttributeError,
# the "Find work" breakage).
from ..reconstruct.reconstruct import (DEFAULT_MODEL as _RECON_MODEL,
                                       _default_model_fn as _recon_model_fn)
from ..discovery import scorer as discovery_scorer
from ..discovery import describe as discovery_describe
from ..discovery.discover import discover as _real_discover
from ..discovery.discover import (DEFAULT_MODEL as _DISCOVER_MODEL,
                                  _default_model_fn as _discover_model_fn)
from ..discovery.discover import load_event_log, render_event_log
from ..discovery.prompt import SYSTEM_PROMPT as DISCOVERY_SYSTEM_PROMPT
from ..artifact import review as artifact_review
from ..artifact.artifact import build_prompt_material, write_spec_bundle
from ..artifact.artifact import generate_specs as _real_generate_specs
from ..artifact.artifact import (DEFAULT_MODEL as _SPECS_MODEL,
                                 _default_model_fn as _specs_model_fn)
from ..artifact.prompt import SYSTEM_PROMPT as ARTIFACT_SYSTEM_PROMPT
from ..agent import configgen as agent_configgen
from ..agent import interview as agent_interview
from ..agent import connect as agent_connect
from ..agent import config as agent_config
from ..agent import deploy as agent_deploy
from ..agent import runner as agent_runner
from ..agent import activity as agent_activity
from ..agent import embeddings as agent_embeddings
from ..agent import generalize as agent_generalize
from . import cloud, keystore, permissions, registry, sessions, supabase_client, users


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# The worker's free-text note rides in the same answers list as a reserved entry, so it
# reaches the agent config through the existing path (configgen._load_answers) with no
# plumbing of its own. It is deliberately NOT a question: it never counts as unresolved
# and never gates the step.
EXTRA_NOTES_ID = "extra_notes"
EXTRA_NOTES_LABEL = "Anything else we should know about this workflow?"


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
# Phase B (B5d): a cross-user lesson confirmed by this many DISTINCT users graduates to a
# global auto-trusted default — it then injects without the per-agent confirm checkpoint.
FLEET_DEFAULT_CONFIRMATIONS = 5

# Group-chat guardrails (credit protection). Each group message can trigger model calls
# (a broadcast = one per member; an @mention = a full agent session), so cap the blast
# radius and rate-limit per user. Tune via env if needed.
GROUP_MSG_RATE = int(os.environ.get("TRACE_GROUP_MSG_RATE", "20"))     # messages
GROUP_MSG_WINDOW = int(os.environ.get("TRACE_GROUP_MSG_WINDOW", "60"))  # ...per this many seconds
MAX_MENTIONS_PER_MESSAGE = 3     # bound the expensive @mention session fan-out
MAX_BROADCAST_MEMBERS = 10       # bound the read-only Haiku fan-out
ADMIN_STATIC_DIR = os.path.join(os.path.dirname(__file__), "admin_static")  # admin portal
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
                 discovery_fn=None, describe_fn=None, score_fn=None, artifact_fn=None,
                 interview_fn=None, config_fn=None, cleanup_fn=None,
                 connect_fn=None, deploy_fn=None, cloud_backend=None, proxy_fn=None,
                 summarize_fn=None):
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
        # Skip-recording path: typed description -> discovery contract (injectable).
        self.describe_fn = describe_fn or discovery_describe.describe_workflows
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
        # Activity view: a model_fn(system, user)->str that summarizes session logs into
        # plain language (Haiku). Injected as a stub in tests; built lazily from the key.
        self.summarize_fn = summarize_fn
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
        self.describe_jobs = {}      # sid -> {state, error, bundles, anti_bundles}
        self.spec_jobs = {}          # sid -> {state, error, built, notes}
        self.interview_jobs = {}     # sid -> {state, error, generated}
        self.build_jobs = {}         # sid -> {state, error, generated, errors}
        self.connection_jobs = {}    # sid -> {state, error, slug, connect_url, verified, flag}
        self.deploy_jobs = {}        # sid -> {state, error, deployed, errors}
        self.scheduler_stop = threading.Event()   # Spec 4: Trace-side scheduler
        self._scheduler_thread = None
        self._vault_refreshed = {}   # vault_ref -> epoch of last rolling refresh (deployment sync)
        self.chat_jobs = {}          # agent_id -> {state, error} (a DM reply in flight)
        self._msg_rate = {}          # uid -> [recent message epochs] (group-chat rate limit)
        # Admin portal: live-only LLM prompt log (a bounded in-memory ring; nothing is
        # persisted — prompts embed capture-derived user data). Served exclusively
        # through the token-gated /api/admin/ surface.
        self.prompt_log = collections.deque(maxlen=200)
        self.prompt_seq = 0
        self.prompt_lock = threading.Lock()
        self.started_at = time.time()
        self.lock = threading.Lock()
        os.makedirs(sessions_root, exist_ok=True)

    def _default_cleanup(self, session_ids, file_ids, agent_ids, agent_names=None,
                         deployment_ids=None, memory_store_ids=None):
        """Real, human-initiated platform cleanup: requires a key; builds the client and
        deletes the Trace-created ids it is given (sessions, files, agents, deployments,
        memory stores), plus any agents the platform still has under this session's
        workflow names (``agent_names`` — sweeps orphaned/stale ones)."""
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        from ..agent.api_client import ManagedAgentsClient
        client = ManagedAgentsClient(api_key=key)
        return client.cleanup(session_ids=session_ids, file_ids=file_ids,
                              agent_ids=agent_ids, agent_names=agent_names,
                              deployment_ids=deployment_ids,
                              memory_store_ids=memory_store_ids)

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
                                        trigger=start.get("trigger"), schedule=start.get("schedule"),
                                        staged_apps=start.get("staged_apps"))

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

                kw = {}
                if self.reconstruct_fn is _real_reconstruct:   # tap the real engine's calls
                    kw["model_fn"] = self._tap(
                        "reconstruct", sid, _RECON_MODEL,
                        lambda: _recon_model_fn(_RECON_MODEL))
                log, report = self.reconstruct_fn(raw_path, progress_cb=cb, **kw)
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
                # Sync to Supabase: validation + event_log
                val_data = {"errors": report.errors, "flags": report.flags}
                sessions.write_json_artifact(self.sessions_root, sid,
                                             "validation_report", val_data)
                sessions.sync_to_supabase(self.sessions_root, sid,
                                          reconstructed_at=sessions._now(),
                                          event_count=len(log))
                meta = sessions.read_meta(self.sessions_root, sid)
                sessions._upload_to_storage(self.sessions_root, sid,
                                            sessions.EVENT_LOG,
                                            meta.get("owner_user_id") or "")
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
                kw = {}
                if self.discovery_fn is _real_discover:        # tap the real engine's call
                    kw["model_fn"] = self._tap(
                        "discovery", sid, _DISCOVER_MODEL,
                        lambda: _discover_model_fn(_DISCOVER_MODEL))
                discovery, report = self.discovery_fn(log_path, **kw)
                with open(sessions.path(self.sessions_root, sid, sessions.DISCOVERY),
                          "w", encoding="utf-8") as f:
                    json.dump(discovery, f, indent=2, ensure_ascii=False)
                sessions.update_meta(self.sessions_root, sid,
                                     discovered_at=sessions._now())
                # Sync discovery to Supabase
                sessions.write_json_artifact(self.sessions_root, sid,
                                             "discovery", discovery)
                sessions.sync_to_supabase(self.sessions_root, sid,
                                          discovered_at=sessions._now())
                job.update(state="done", bundles=len(discovery["bundles"]),
                           anti_bundles=len(discovery["anti_bundles"]))
            except Exception as e:
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()

    # --- describe job (skip-recording path) -------------------------------
    def start_describe(self, sid, description):
        """Turn a typed workflow description into this session's discovery artifacts.
        Writes ``event_log.json = []`` and ``discovery.json`` TOGETHER at job end so a
        mid-job reload never lands on the Workflows step and races an empty ``/discover``
        against the real result (an empty log short-circuits discovery to nothing)."""
        job = {"state": "running", "error": None, "bundles": 0, "anti_bundles": 0}
        self.describe_jobs[sid] = job

        def run():
            try:
                key = self.keystore.get_key()
                if not key:
                    raise RuntimeError("no_api_key")
                os.environ["ANTHROPIC_API_KEY"] = key
                kw = {}
                if self.describe_fn is discovery_describe.describe_workflows:
                    kw["model_fn"] = self._tap(       # tap the real engine's call
                        "describe", sid, _DISCOVER_MODEL,     # describe reuses discover's caller
                        lambda: _discover_model_fn(_DISCOVER_MODEL))
                discovery, report = self.describe_fn(description, **kw)
                _write_json(sessions.path(self.sessions_root, sid, sessions.EVENT_LOG), [])
                _write_json(sessions.path(self.sessions_root, sid, sessions.DISCOVERY),
                            discovery)
                sessions.update_meta(self.sessions_root, sid,
                                     describe_at=sessions._now(),
                                     discovered_at=sessions._now(), event_count=0)
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

                kw = {}
                if self.artifact_fn is _real_generate_specs:   # tap the real engine's calls
                    kw["model_fn"] = self._tap(
                        "specs", sid, _SPECS_MODEL,
                        lambda: _specs_model_fn(_SPECS_MODEL))
                results, report = self.artifact_fn(reviewed_path, event_log_path, **kw)
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

    _PROMPT_CLIP = 20_000        # per-field cap so 200 entries stay bounded in memory

    def _clip(self, text):
        s = "" if text is None else str(text)
        return s if len(s) <= self._PROMPT_CLIP else s[:self._PROMPT_CLIP] + "\n… [truncated]"

    def _tap(self, engine, sid, model, make_fn):
        """Wrap a model caller so every (system, user) -> response is recorded in the
        in-memory prompt log for the admin portal. ``make_fn()`` builds the real caller
        lazily on first use (preserving the lazy-SDK property of the default builders).
        Errors are recorded AND re-raised — the tap never changes behavior."""
        holder = {}

        def call(system, user):
            if "fn" not in holder:
                holder["fn"] = make_fn()
            t0 = time.monotonic()
            entry = {"ts": sessions._now(), "engine": engine, "sid": sid, "model": model,
                     "system": self._clip(system), "user": self._clip(user),
                     "response": None, "error": None, "ms": None}
            try:
                out = holder["fn"](system, user)
                entry["response"] = self._clip(out)
                return out
            except Exception as e:                        # noqa: BLE001
                entry["error"] = f"{type(e).__name__}: {e}"
                raise
            finally:
                entry["ms"] = int((time.monotonic() - t0) * 1000)
                with self.prompt_lock:
                    self.prompt_seq += 1
                    entry["seq"] = self.prompt_seq
                    self.prompt_log.append(entry)
        return call

    def _build_model_fn(self, engine="config", sid=None):
        """A Sonnet caller from the saved key, for the interview + config engines.
        Raises ``no_api_key`` when no key is saved. The ``anthropic`` client is built
        lazily on first call, so injected stubs that never call it run without the SDK.
        Every call is recorded in the admin prompt log."""
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        return self._tap(engine, sid, agent_configgen.DEFAULT_MODEL,
                         lambda: agent_configgen._default_model_fn(agent_configgen.DEFAULT_MODEL))

    def _summarize_model_fn(self):
        """The injected summarizer if present (tests), else a lazy Haiku caller from the
        saved key. Raises ``no_api_key`` only if it actually needs to call the model.
        Either way the call is recorded in the admin prompt log."""
        if self.summarize_fn:
            return self._tap("activity", None, agent_config.SUMMARY_MODEL,
                             lambda: self.summarize_fn)
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        return self._tap("activity", None, agent_config.SUMMARY_MODEL,
                         lambda: agent_configgen._default_model_fn(agent_config.SUMMARY_MODEL))

    def agent_available_apps(self, agent):
        """The agent's usable connected apps, each flagged whether it's currently staged
        (held for approval). Drives the post-deploy staging toggle. Reads the agent's
        session connection record; on any failure falls back to the currently-staged set
        so the toggle can still render what's staged."""
        staged = set(s for s in (agent.get("staged_apps") or []) if s)
        try:
            rec, _ = self._load_connection(agent.get("session_id"))
            usable = agent_deploy._usable_app_slugs(rec)
        except Exception:                                  # noqa: BLE001 — best-effort
            usable = sorted(staged)
        # keep any staged slug even if the connection record no longer lists it as usable
        slugs = list(dict.fromkeys(list(usable) + sorted(staged)))
        return [{"slug": s, "label": agent_runner._app_label(s), "staged": s in staged}
                for s in slugs]

    def agent_activity(self, agent, limit=10):
        """The agent's recent runs as REAL session activity: for each run's session id,
        read the session's events from the platform, summarize them in plain language
        (cached per session), classify the outcome, and surface any staged proposals as
        approvable items. Authoritative (ground truth), unlike the run self-report.
        Best-effort per session — one bad fetch never breaks the list."""
        root = self.sessions_root
        owner = agent.get("owner_user_id")
        staged = bool(agent.get("staged_apps"))
        try:
            client = self._live_client()
        except Exception:
            client = None
        try:
            model_fn = self._summarize_model_fn()
        except Exception:
            model_fn = None
        out = []
        seen = set()
        for run in registry.runs_for_agent(root, agent.get("agent_id"), limit):
            sid = run.get("run_session_id")
            entry = {"ran_at": run.get("ran_at"), "run_session_id": sid,
                     "status": run.get("status"), "summary": run.get("summary") or "",
                     "proposals": [], "decisions": []}
            if not sid or sid in seen or client is None:
                out.append(entry)
                continue
            seen.add(sid)
            try:
                events = client.list_session_events(sid)
            except Exception:
                out.append(entry)
                continue
            entry["status"] = agent_activity.session_status(events)
            cached = registry.summary_get(root, sid)
            if cached and cached.get("summary"):
                entry["summary"] = cached["summary"]
            elif model_fn is not None and agent_activity.is_terminal(events):
                summ = agent_activity.summarize_session(events, agent.get("name"), model_fn)
                if summ:
                    entry["summary"] = summ
                    registry.summary_set(root, sid, summ, entry["status"])
            if staged:                                     # surface proposals as approvable items
                entry["proposals"] = self._session_proposals(agent, sid, events, owner)
            if entry["status"] == "needs_input":           # Gap #3: surface open questions
                entry["decisions"] = self._session_decisions(agent, sid, events, owner)
            out.append(entry)
        return out

    def _session_proposals(self, agent, run_session_id, events, owner):
        """Persist this session's proposed actions as pending items (deduped) and return
        them (with their stored status) for the activity screen. Falls back to the final
        message text when the agent didn't emit a structured block, so nothing is lost."""
        root = self.sessions_root
        agent_id = agent.get("agent_id")
        proposals = agent_activity.extract_proposals(events)
        if not proposals:
            final = agent_runner._final_text(events)
            if final.strip() and agent_activity.is_terminal(events):
                proposals = [{"app": "", "action": "review",
                              "summary": agent_runner._first_line(final) or "Review the proposal",
                              "details": {"description": final}}]
        for it in agent_runner._proposals_to_pending(agent_id, proposals, run_session_id):
            registry.pending_add(root, {
                "agent_id": agent_id, "owner_user_id": owner,
                "run_session_id": run_session_id, "app": it.get("app"),
                "kind": it.get("kind"), "summary": it.get("summary"),
                "rationale": it.get("rationale"),
                "payload": it.get("payload"), "content_hash": it.get("content_hash")})
        return [p for p in registry.pending_for_user(root, owner, status="pending")
                if p.get("run_session_id") == run_session_id]

    def _persist_decisions(self, agent, run_session_id, questions, owner):
        """Gap #3: persist a run's needs_input questions as answerable decision records
        (deduped), returning the still-open ones for this session for the UI."""
        root = self.sessions_root
        tags = {"bundle_id": agent.get("bundle_id") or "",
                "workflow": agent.get("name") or agent.get("bundle_id") or ""}
        for q in questions or []:
            if not isinstance(q, dict):
                continue
            qt = str(q.get("question") or "").strip()
            if not qt:
                continue
            registry.decision_add(root, {
                "agent_id": agent.get("agent_id"), "owner_user_id": owner,
                "run_session_id": run_session_id, "question": qt,
                "context": {"context": str(q.get("context") or ""),
                            "options": [str(o) for o in (q.get("options") or [])]},
                "tags": tags})
        return [d for d in registry.decisions_for_user(root, owner, status="pending")
                if d.get("run_session_id") == run_session_id]

    def _session_decisions(self, agent, run_session_id, events, owner):
        """Persist + return this session's needs_input questions (activity-view path)."""
        return self._persist_decisions(
            agent, run_session_id, agent_activity.extract_questions(events), owner)

    def resume_after_answer(self, agent, decision, answer):
        """Gap #3 auto-immediate resume: re-run the agent seeded with the paused question
        and the human's answer so it continues where it stopped, then mark the decision
        resolved and learn a reusable lesson from it. Never raises (a run failure is
        already recorded by run_agent)."""
        ctx = decision.get("context")
        resume = {"question": decision.get("question"),
                  "context": ctx.get("context") if isinstance(ctx, dict) else "",
                  "answer": answer}
        try:
            rec = self.run_agent(agent, resume=resume)
        except Exception:                                  # noqa: BLE001
            rec = None
        registry.decision_set_status(self.sessions_root, decision.get("id"), "resolved")
        self._learn_from_decision(decision, answer, agent)
        return rec

    @staticmethod
    def _agent_kind_text(agent):
        """A short description of an agent's KIND (workflow), used for the agent-similarity
        gate so a shipping lesson never reaches an invoice agent."""
        return (f"{agent.get('name') or ''}. {agent.get('description') or ''}. "
                f"Does: {agent.get('trigger') or ''}").strip()

    def _embed_agent_kind(self, agent):
        """Phase B: embed + store an agent's kind vector (best-effort; no-op without key)."""
        vecs = agent_embeddings.embed(self._agent_kind_text(agent), input_type="document")
        if vecs:
            registry.agent_embedding_upsert(self.sessions_root, agent.get("agent_id"), vecs[0])

    def _retrieve_guidance(self, agent):
        """Gap #3 B4/B5c: retrieve reusable lessons relevant to this agent and return the
        ones to INJECT now. OWN lessons and already-CONFIRMED cross-user lessons inject
        (provenance logged). A matched-but-unconfirmed CROSS-user lesson is NOT injected —
        it's recorded as a provisional promotion so it surfaces as a 'confirm this lesson?'
        checkpoint; confirming it (once) promotes it and it injects on later runs. This is
        the trust gate: a stranger's lesson never silently drives your agent on first sight.
        Best-effort — [] with no embeddings/key."""
        root = self.sessions_root
        aid, owner = agent.get("agent_id"), agent.get("owner_user_id")
        query = f"{agent.get('name') or ''}: {agent.get('trigger') or ''}".strip()
        svecs = agent_embeddings.embed(query, input_type="query")
        if not svecs:
            return []
        scopes = registry.agent_lesson_scopes(root, agent)
        kind_emb = registry.agent_embedding_get(root, aid)
        if kind_emb is None:                               # lazily compute + store if missing
            kvecs = agent_embeddings.embed(self._agent_kind_text(agent), input_type="document")
            if kvecs:
                kind_emb = kvecs[0]
                registry.agent_embedding_upsert(root, aid, kind_emb)
        hits = registry.retrieve_lessons(root, svecs[0], agent, agent_kind_embedding=kind_emb,
                                         consume_scope=scopes["consume_scope"])
        inject = []
        for h in hits:
            if h["tier"] == "own":
                inject.append(h)
                continue
            promo = registry.lesson_promotion_get(root, aid, h["decision_id"])
            status = (promo or {}).get("status")
            if status == "confirmed":
                inject.append(h)
            elif status == "retired":
                continue
            elif registry.lesson_confirmed_user_count(root, h["decision_id"]) >= FLEET_DEFAULT_CONFIRMATIONS:
                # fleet-default: confirmed by many distinct users -> auto-trust, skip checkpoint
                if promo:
                    registry.lesson_promotion_set_status(root, aid, h["decision_id"], "confirmed")
                else:
                    registry.lesson_promotion_upsert(root, aid, owner, h["decision_id"],
                                                     status="confirmed", lesson=h["lesson"])
                inject.append(h)
            else:                                          # surface a one-time confirm checkpoint
                registry.lesson_promotion_upsert(root, aid, owner, h["decision_id"],
                                                 lesson=h["lesson"])
        for h in inject:                                   # provenance: what influenced this run
            registry.lesson_application_add(root, aid, owner, h["decision_id"], None)
        return [h["lesson"] for h in inject]

    def _learn_from_decision(self, decision, answer, agent=None):
        """Gap #3 B3/B5d: distill a de-identified, reusable lesson from an answered decision
        and embed it. If the source consents to share GLOBALLY (contribute scope) AND the
        lesson passes a second scrub-verify pass, mark it globally shareable so OTHER users'
        agents can retrieve it. Best-effort — any failure silently skips and never blocks
        the resume; the global gate FAILS CLOSED (unverified => owner-only)."""
        root = self.sessions_root
        try:
            model_fn = self._summarize_model_fn()
        except Exception:                                  # noqa: BLE001 — no key in dev
            return
        try:
            result = agent_generalize.generalize_decision({**decision, "answer": answer},
                                                          model_fn)
        except Exception:                                  # noqa: BLE001
            return
        did = decision.get("id")
        registry.decision_set_generalization(root, did, result["reusable"], result["lesson"])
        if not (result["reusable"] and result["lesson"]):
            return
        vecs = agent_embeddings.embed(result["lesson"], input_type="document")
        if vecs:
            registry.decision_embedding_add(root, did, vecs[0])
        # T3 gate: only cross-user-shareable if the source consents to global AND it scrubs clean
        contribute = (registry.agent_lesson_scopes(root, agent)["contribute_scope"] if agent
                      else registry.user_consent_get(root, decision.get("owner_user_id"))["contribute_scope"])
        global_ok = (contribute == "global"
                     and agent_generalize.verify_lesson_scrub(result["lesson"], model_fn))
        registry.decision_set_global_verified(root, did, global_ok)

    def approve_actions(self, agent, pending_ids):
        """Approve a chosen set of staged actions: perform them in ONE fresh session and
        mark each executed. Returns the execution record. Never raises."""
        root = self.sessions_root
        items = []
        for pid in pending_ids or []:
            it = registry.pending_get(root, pid)
            if it and it.get("owner_user_id") == agent.get("owner_user_id") and it.get("status") == "pending":
                items.append(it)
        if not items:
            return {"status": "failed", "summary": "Nothing to approve."}
        self._refresh_agent_binding(agent)
        try:
            client = self._live_client()
            res = agent_runner.execute_actions(client, agent, items)
        except Exception:
            res = {"status": "failed", "summary": "Couldn't perform them.", "run_session_id": None}
        new_status = "executed" if res["status"] == "executed" else "failed"
        for it in items:
            registry.pending_set_status(root, it.get("id"), new_status)
        registry.run_add(root, {
            "agent_id": agent.get("agent_id"), "owner_user_id": agent.get("owner_user_id"),
            "status": "completed" if res["status"] == "executed" else "failed",
            "summary": res.get("summary") or "", "details": [it.get("summary") for it in items],
            "run_session_id": res.get("run_session_id")})
        return res

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
                model_fn = self._build_model_fn("interview", sid) if approved else None

                def _questions_for(b):
                    """One workflow's questions. Runs CONCURRENTLY with the others: this
                    is a model call each, and in series three workflows meant three waits
                    on the slowest step of the wizard."""
                    questions = self.interview_fn(b, event_log, model_fn=model_fn)["questions"]
                    # preserve any prior answer whose question still maps — ids are
                    # positional (q1..qN), so the stored question text must match too
                    # or an old answer could silently attach to a different question
                    prior = sessions.read_json_artifact(
                        root, sid, sessions.interview_filename(b["id"])) or {}
                    qtext = {q["id"]: q.get("question", "") for q in questions}
                    answers = [a for a in (prior.get("answers") or [])
                               if a.get("q") in qtext and str(a.get("answer", "")).strip()
                               and a.get("question", "") == qtext[a.get("q")]]
                    # the free-text note isn't tied to any question, so regenerating the
                    # questions must not throw it away
                    answers += [a for a in (prior.get("answers") or [])
                                if a.get("q") == EXTRA_NOTES_ID
                                and str(a.get("answer", "")).strip()]
                    rec = _interview_record(b["id"], questions, answers)
                    _write_json(sessions.path(root, sid, sessions.interview_filename(b["id"])), rec)
                    return b["id"]

                done = self._fanout([lambda b=b: _questions_for(b) for b in approved])
                job["generated"] = sum(1 for d in done if d)
                failed = [b["id"] for b, d in zip(approved, done) if d is None]
                if failed:
                    # Surface which workflows failed instead of aborting the whole step —
                    # the ones that succeeded keep their questions.
                    raise RuntimeError("couldn't prepare questions for: " + ", ".join(failed))
                sessions.update_meta(root, sid, interview_at=sessions._now())
                self._sync_interview_complete(sid)   # zero-approved => vacuously complete
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
        # Names, so the form can title each workflow the way the worker would recognize
        # it. Without these the UI fell back to the bundle id and asked people about
        # "B1" and "B2" — internal labels that mean nothing to them. The review-step
        # correction wins where it exists, since that is the worker's own wording.
        names = {b["id"]: (b.get("user_description") or b.get("name") or b["id"])
                 for b in approved}
        return {"approved_ids": [b["id"] for b in approved], "interviews": out,
                "names": names}

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
        extra = str((answers_map or {}).get(EXTRA_NOTES_ID, "") or "").strip()
        if extra:                                      # free-text: optional, never gating
            answers.append({"q": EXTRA_NOTES_ID, "question": EXTRA_NOTES_LABEL,
                            "answer": extra})
        rec = _interview_record(bid, questions, answers)
        _write_json(sessions.path(self.sessions_root, sid, sessions.interview_filename(bid)), rec)
        self._sync_interview_complete(sid)
        return rec

    def _interview_all_complete(self, sid):
        """True when EVERY approved bundle's interview record exists with status
        'complete' (vacuously true with zero approved bundles). The questions phase
        is mandatory: this is the build gate, always recomputed from disk."""
        approved, _ = self._approved_for_stage4(sid)
        for b in approved:
            rec = sessions.read_json_artifact(self.sessions_root, sid,
                                              sessions.interview_filename(b["id"]))
            if not rec or rec.get("status") != "complete":
                return False
        return True

    def _sync_interview_complete(self, sid):
        """Mirror the completeness state into meta ``interview_complete_at`` so the UI's
        step derivation can gate on it (presentation only — the build gate recomputes)."""
        done = self._interview_all_complete(sid)
        sessions.update_meta(self.sessions_root, sid,
                             interview_complete_at=(sessions._now() if done else None))
        return done

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
                model_fn = self._build_model_fn("config", sid) if approved else None
                def _build_one(b):
                    """One workflow's config. Concurrent for the same reason as the
                    questions step; each bundle already records its own error."""
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
                        return bid
                    except Exception as e:
                        job["errors"][bid] = f"{type(e).__name__}: {e}"
                        return None

                built = self._fanout([lambda b=b: _build_one(b) for b in approved])
                job["generated"] = sum(1 for x in built if x)
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
        deployment_ids, memory_store_ids = [], []
        # the agent names Trace uses for this session's bundles (matches configgen's
        # _envelope: name -> bundle name/id), so the sweep can reach stale duplicates.
        approved, _ = self._approved_for_stage4(sid)
        agent_names = list(dict.fromkeys(
            str(b.get("name") or b.get("id") or "Agent").strip() for b in approved))
        # live deploy records (Phase 6): the live sessions + agents + deployments + memory
        for d in sessions.list_deploy_records(self.sessions_root, sid):
            if d.get("session_id"):
                session_ids.append(d["session_id"])
            if d.get("agent_id"):
                agent_ids.append(d["agent_id"])
            if d.get("deployment_id"):
                deployment_ids.append(d["deployment_id"])
            if d.get("memory_store_id"):
                memory_store_ids.append(d["memory_store_id"])
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
        deployment_ids = list(dict.fromkeys(deployment_ids))
        memory_store_ids = list(dict.fromkeys(memory_store_ids))
        kw = {}
        if deployment_ids:
            kw["deployment_ids"] = deployment_ids
        if memory_store_ids:
            kw["memory_store_ids"] = memory_store_ids
        try:
            result = self.cleanup_fn(session_ids, file_ids, agent_ids,
                                     agent_names=agent_names, **kw)
        except TypeError:          # injected stub with the pre-deployment signature
            result = self.cleanup_fn(session_ids, file_ids, agent_ids, agent_names=agent_names)
        return {"requested": {"sessions": session_ids, "files": file_ids,
                              "agents": agent_ids, "agent_names": agent_names,
                              "deployments": deployment_ids,
                              "memory_stores": memory_store_ids},
                **(result or {})}

    def delete_capture_data(self, sid, owner_user_id):
        """User-facing "delete my capture data": remove one workflow's capture session
        (raw recording + every derived artifact) from disk, plus the capture-derived
        files / test sessions on the platform — while the LIVE agent keeps running (its
        durable registry row, vault, schedule, and run history are untouched; the run
        path never reads the session dir). Owner-scoped. Best-effort on the platform
        side (an error there never blocks the on-disk delete)."""
        root = self.sessions_root
        meta = sessions.read_meta(root, sid)
        if not meta or meta.get("owner_user_id") != owner_user_id:
            return {"error": "not_found"}
        # Capture-derived data on the platform = uploaded files + test/manual-eval run
        # sessions. NOT the live agent/session ids (no agent_ids/agent_names), so a live
        # agent is never taken down. Gather before rmtree (this reads the run records).
        file_ids, test_sessions = [], []
        for r in sessions.list_runs(root, sid):
            for s in r.get("sessions", []):
                if s.get("session_id"):
                    test_sessions.append(s["session_id"])
            file_ids.extend(r.get("uploaded_file_ids", []) or [])
        file_ids = list(dict.fromkeys(file_ids))
        test_sessions = list(dict.fromkeys(test_sessions))
        platform = None
        if file_ids or test_sessions:
            try:
                platform = self.cleanup_fn(test_sessions, file_ids, [], agent_names=[])
            except Exception as e:                        # noqa: BLE001
                platform = {"error": f"{type(e).__name__}: {e}"}
        shutil.rmtree(sessions.session_dir(root, sid), ignore_errors=True)
        return {"session_id": sid, "deleted": True, "platform": platform}

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
                # `reused` = this customer already authorized the app (Pipedream accounts
                # are per external_user_id, stable across sessions), so there is no
                # connect_url to open — the UI goes straight to Verify.
                job.update(state="done", connect_url=tok.get("connect_url"),
                           reused=bool(tok.get("reused")),
                           identity_email=tok.get("identity_email"))
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
                    rec = None                       # set once the platform agent exists
                    try:
                        with open(os.path.join(specs_dir, sessions.agent_yaml_name(bid, "live")),
                                  encoding="utf-8") as f:
                            live_dict = yaml.safe_load(f.read())
                        rec = self.deploy_fn(b, live_dict, conn, start)
                        _write_json(os.path.join(specs_dir, sessions.deploy_record_name(bid)), rec)
                        # Spec 2/3/4: durable per-user agent index (survives restart),
                        # so a re-run adds to the user's agent set without clobbering
                        # prior agents, and schedules/activity have something to key on.
                        if rec.get("status") == "live" and rec.get("agent_id"):
                            owner = (sessions.read_meta(root, sid) or {}).get("owner_user_id")
                            if owner:
                                # A redeploy deleted+recreated the agent, stranding any
                                # prior deployment — archive it so the Console stays clean.
                                try:
                                    prior = next(
                                        (a for a in registry.agents_for_user(root, owner)
                                         if a.get("bundle_id") == bid
                                         and a.get("deployment_id")
                                         and a.get("deployment_id") != rec.get("deployment_id")),
                                        None)
                                    if prior:
                                        self._live_client().archive_deployment(
                                            prior["deployment_id"])
                                except Exception:          # noqa: BLE001 — best-effort
                                    pass
                                registry.agent_upsert(root, {
                                    "agent_id": rec["agent_id"], "owner_user_id": owner,
                                    "external_user_id": rec.get("external_user_id"),
                                    "bundle_id": bid, "name": b.get("name") or bid,
                                    "vault_ref": rec.get("vault_ref"),
                                    "trigger": (rec.get("start") or {}).get("value"),
                                    "session_id": sid,
                                    "staged_apps": rec.get("staged_apps") or [],
                                    "deployment_id": rec.get("deployment_id"),
                                    "memory_store_id": rec.get("memory_store_id"),
                                    # the worker's review-step correction is the truer bio
                                    "description": b.get("user_description")
                                                   or b.get("description") or ""})
                                try:                  # Phase B: agent-kind gate. Best-effort
                                    self._embed_agent_kind({   # — an embedding hiccup must
                                        "agent_id": rec["agent_id"],   # never fail a deploy
                                        "name": b.get("name") or bid,
                                        "description": b.get("user_description")
                                                       or b.get("description") or "",
                                        "trigger": (rec.get("start") or {}).get("value")})
                                except Exception:      # noqa: BLE001 — best-effort
                                    traceback.print_exc()
                        job["deployed"] += 1
                    except Exception as e:
                        # Anything that fails AFTER deploy_fn leaves a REAL agent live on
                        # the platform, so say so instead of reporting a plain failure.
                        # Log the traceback too: job["errors"] is in-memory and reaches
                        # neither the logs nor the UI, which makes this class of failure
                        # invisible (a uuid/text column mismatch cost an hour this way).
                        detail = f"{type(e).__name__}: {e}"
                        orphan = (rec or {}).get("agent_id")
                        if orphan:
                            detail += (f" — platform agent {orphan} IS LIVE but was not "
                                       "registered, so it will not appear in the UI")
                        job["errors"][bid] = detail
                        print(f"[deploy] bundle {bid} failed: {detail}", flush=True)
                        traceback.print_exc()
                # Only mark the session deployed if at least one agent was actually
                # created; never report a total failure as "live" (the per-bundle
                # platform error is surfaced so the real cause is visible).
                if job["deployed"]:
                    sessions.update_meta(root, sid, deploy_at=sessions._now())
                    job.update(state="done")
                elif job["errors"]:
                    job.update(state="error",
                               error="No agents were created. " +
                                     "; ".join(f"{bid}: {msg}" for bid, msg in job["errors"].items()))
                else:
                    job.update(state="done")          # nothing eligible -> nothing to do
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

    # --- Spec 4: Trace-side scheduler ------------------------------------
    def start_scheduler(self, interval=60):
        """Start the daemon loop that runs due agents on their cadence. No-op on the
        desktop node (it proxies pipeline calls to the cloud, where the scheduler
        already runs) and idempotent."""
        if self.cloud_backend or self._scheduler_thread is not None:
            return

        def loop():
            while not self.scheduler_stop.wait(interval):
                try:
                    self.run_due_schedules()
                except Exception:
                    pass                                  # a bad cycle never kills the loop

        self._scheduler_thread = threading.Thread(target=loop, daemon=True)
        self._scheduler_thread.start()

    def run_due_schedules(self, now=None):
        """One scheduler pass: run every due LEGACY schedule (agents without a platform
        deployment — the platform crons deployment agents itself, so running them here
        would double-fire), then sync platform-initiated deployment runs into the run
        log. Pure of any thread/HTTP state so it's unit-testable directly."""
        now = time.time() if now is None else now
        for agent in registry.schedule_list_active(self.sessions_root):
            # The platform crons a deployment agent ONLY when its cadence maps to a
            # platform cron (daily/weekly/hourly). A deployment agent whose cadence has
            # no cron mapping (e.g. legacy interval) falls through to the Trace-side
            # scheduler here, so it still fires rather than silently never running.
            if agent.get("deployment_id") and agent_config.cadence_to_cron(agent.get("cadence")):
                continue
            if agent_runner.schedule_due(agent.get("cadence"), agent.get("last_run_epoch"), now):
                self.run_agent(agent)
        self.sync_deployment_runs(now)

    def _record_deployment_run(self, agent, drun_id, status, summary, run_session_id):
        """Record ONE platform run, at most once across the scheduler sync pass and
        Run-now — both can observe the same deployment_run. The seen-check and the
        insert happen under one lock so the check-then-add is atomic (drun_id has no
        DB uniqueness constraint). A None drun_id (a run that never started) is always
        recorded. Returns True if it wrote a row."""
        aid = agent.get("agent_id")
        with self.lock:
            if drun_id and drun_id in registry.run_seen_drun_ids(self.sessions_root, aid):
                return False
            registry.run_add(self.sessions_root, {
                "agent_id": aid, "owner_user_id": agent.get("owner_user_id"),
                "status": status, "summary": summary, "details": [],
                "run_session_id": run_session_id, "drun_id": drun_id})
            return True

    # Rolling vault-refresh throttle: platform cron fires without Trace in the loop,
    # and the Pipedream dev token lives ~1h — refreshing each deployment agent's vault
    # at most every 30 min keeps the credential inside its TTL for whenever a run fires.
    VAULT_REFRESH_SECONDS = 30 * 60

    def sync_deployment_runs(self, now=None):
        """Deployment sync pass: for every agent backed by a platform deployment,
        (a) keep its vault token fresh (throttled), and (b) harvest deployment_runs
        Trace hasn't seen into the run log (keyed by drun_id), so the activity view —
        and its proposal/staging extraction, which reads the real session events by
        run_session_id — covers platform-initiated runs. Per-agent failures never
        kill the pass."""
        now = time.time() if now is None else now
        agents = registry.agents_with_deployments(self.sessions_root)
        if not agents:
            return
        for agent in agents:
            try:
                vault_ref = agent.get("vault_ref")
                last = self._vault_refreshed.get(vault_ref, 0)
                if vault_ref and now - last >= self.VAULT_REFRESH_SECONDS:
                    self._refresh_agent_binding(agent)
                    self._vault_refreshed[vault_ref] = now
                client = self._live_client()
                agent_id = agent.get("agent_id")
                fresh = False
                for drun in client.list_deployment_runs(agent.get("deployment_id")):
                    if not drun.get("id"):
                        continue
                    if drun.get("error_type"):
                        status = "failed"
                        summary = (f"Scheduled run couldn't start: "
                                   f"{drun.get('error_message') or drun['error_type']}")
                    else:
                        status = "ran"
                        summary = "Scheduled run started — open Activity for what it did."
                    if self._record_deployment_run(agent, drun["id"], status, summary,
                                                   drun.get("session_id")):
                        fresh = True
                        if status == "failed":              # DM only on a genuinely-new failure
                            self._agent_dm(agent, f"My last run hit a problem: {summary}")
                if fresh:
                    registry.schedule_mark_run(self.sessions_root, agent_id)
            except Exception:                              # noqa: BLE001
                pass                                       # one bad agent never kills the pass

    def run_deployment_now(self, agent):
        """Manual run of a deployment-backed agent: refresh the vault, trigger a
        platform run, and record it. Never raises (a failure becomes a failed run)."""
        self._refresh_agent_binding(agent)
        self._vault_refreshed[agent.get("vault_ref")] = time.time()
        try:
            drun = self._live_client().run_deployment(agent.get("deployment_id"))
            if drun.get("error_type"):
                rec = {"status": "failed",
                       "summary": (f"Couldn't start the run: "
                                   f"{drun.get('error_message') or drun['error_type']}"),
                       "run_session_id": drun.get("session_id"), "drun_id": drun.get("id")}
            else:
                rec = {"status": "ran",
                       "summary": "Run started — open Activity for what it did.",
                       "run_session_id": drun.get("session_id"), "drun_id": drun.get("id")}
        except Exception as e:                              # noqa: BLE001
            rec = {"status": "failed", "summary": f"Couldn't start the run: {type(e).__name__}",
                   "run_session_id": None, "drun_id": None}
        # Dedup-safe record: the scheduler sync pass can list this same drun_id
        # concurrently — the locked recorder ensures it lands exactly once.
        self._record_deployment_run(agent, rec.get("drun_id"), rec["status"], rec["summary"],
                                    rec.get("run_session_id"))
        registry.schedule_mark_run(self.sessions_root, agent.get("agent_id"))
        if rec["status"] == "failed":
            self._agent_dm(agent, f"My last run hit a problem: {rec['summary']}")
        return rec

    # --- Instagram-style "My agents": DMs + home feed ---------------------
    def _agent_dm(self, agent, text):
        """Record a proactive message FROM the agent to its owner (failed runs,
        staged approvals). Best-effort — a store error never breaks a run path."""
        try:
            registry.message_add(self.sessions_root, {
                "agent_id": agent.get("agent_id"),
                "owner_user_id": agent.get("owner_user_id"),
                "sender": "agent", "text": text})
        except Exception:                                  # noqa: BLE001
            pass

    def send_agent_message(self, agent, text):
        """User -> agent DM: record the message, then get a REAL reply by running a
        fresh chat session with the agent (memory attached so it can consult its own
        status/lessons; vault attached so read tools work). The agent's final message
        becomes its reply. Async via chat_jobs[agent_id]; never raises — a platform
        failure becomes an apologetic agent message so the thread never goes silent."""
        root = self.sessions_root
        aid, owner = agent.get("agent_id"), agent.get("owner_user_id")
        registry.message_add(root, {"agent_id": aid, "owner_user_id": owner,
                                    "sender": "user", "text": text, "read": True})
        job = {"state": "running", "error": None}
        self.chat_jobs[aid] = job

        def run():
            reply, err = "", None
            try:
                self._refresh_agent_binding(agent)
                client = self._live_client()
                history = registry.messages_for_agent(root, owner, aid, limit=12)
                context = "\n".join(
                    f"{'Them' if m.get('sender') == 'user' else 'You'}: {m.get('text')}"
                    for m in history[:-1][-8:]) or "(no prior messages)"
                instruction = (agent_config.CHAT_DIRECTIVE
                               .replace("{context}", context).replace("{text}", text))
                env = client.create_environment()
                sid = client.create_session(
                    aid, env, resources=agent_runner._memory_resources(agent),
                    title="Trace direct message",
                    vault_ids=[agent["vault_ref"]] if agent.get("vault_ref") else None)
                events = client.send_trigger_and_collect(sid, instruction)
                reply = (agent_runner._final_text(events) or "").strip()
            except Exception as e:                          # noqa: BLE001
                err = str(e)
                reply = (f"I couldn't reply just now ({type(e).__name__}) — "
                         "give me a minute and try again.")
            if not reply:
                reply = "I finished looking but had nothing to say — try rephrasing?"
            registry.message_add(root, {"agent_id": aid, "owner_user_id": owner,
                                        "sender": "agent", "text": reply})
            # Flip the job LAST, after the reply is persisted — so anything polling
            # `replying` (the UI, or a test) only sees "done" once the reply exists.
            job.update(state=("error" if err else "done"), error=err)

        threading.Thread(target=run, daemon=True).start()

    # --- group chat: read-only broadcast Q&A -------------------------------
    def msg_rate_ok(self, uid):
        """Per-user group-message rate limit (credit guard). Sliding window; returns False
        when the user has sent GROUP_MSG_RATE messages within GROUP_MSG_WINDOW seconds."""
        now = time.time()
        recent = [t for t in self._msg_rate.get(uid, []) if now - t < GROUP_MSG_WINDOW]
        if len(recent) >= GROUP_MSG_RATE:
            self._msg_rate[uid] = recent
            return False
        recent.append(now)
        self._msg_rate[uid] = recent
        return True

    def _agent_run_context(self, agent, limit=8, detail_lines=6):
        """A compact text digest of an agent's recent runs — the grounding for a read-only
        Q&A. No platform calls; uses stored run records.

        Includes each run's ACTIONS and anything it staged, not just the one-line summary.
        With summaries alone an agent could say "I ran two jobs today" but not what was in
        them, so asking it anything concrete got "you'd need to open the Activity logs" —
        the specifics were sitting in the run record, just never handed over.
        """
        root = self.sessions_root
        aid = agent.get("agent_id")
        staged_by_session = {}
        try:                                               # what it proposed, per run
            for p in registry.pending_for_user(root, agent.get("owner_user_id")):
                if p.get("agent_id") == aid and p.get("run_session_id"):
                    staged_by_session.setdefault(p["run_session_id"], []).append(
                        str(p.get("summary") or "")[:200])
        except Exception:                                  # noqa: BLE001 — best-effort
            staged_by_session = {}
        lines = []
        for r in registry.runs_for_agent(root, aid, limit):
            sid = r.get("run_session_id")
            summ = (registry.summary_get(root, sid) or {}).get("summary") if sid else None
            note = summ or r.get("summary") or ""
            lines.append(f"- [{r.get('ran_at', '')}] {r.get('status', '')}: {note}")
            for d in (r.get("details") or [])[:detail_lines]:
                text = d if isinstance(d, str) else (
                    (d or {}).get("summary") or (d or {}).get("action") or "")
                if str(text).strip():
                    lines.append(f"    · did: {str(text).strip()[:200]}")
            for s in staged_by_session.get(sid, [])[:detail_lines]:
                if s.strip():
                    lines.append(f"    · staged for approval: {s.strip()}")
        return "\n".join(lines) or "(no runs yet)"

    @staticmethod
    def _fanout(thunks, workers=None):
        """Run independent, IO-bound calls concurrently and return results IN ORDER.

        Every fan-out here is one network call per agent — model calls for a broadcast,
        platform sessions for @mentions — so they were waiting on the sum of the parts
        for no reason. A thunk that raises yields None rather than losing the whole set;
        callers substitute their own fallback.
        """
        thunks = list(thunks or [])
        if not thunks:
            return []
        if len(thunks) == 1:                               # no thread for a single call
            try:
                return [thunks[0]()]
            except Exception:                              # noqa: BLE001
                return [None]

        def _safe(fn):
            try:
                return fn()
            except Exception:                              # noqa: BLE001
                return None

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers or len(thunks)) as pool:
            return list(pool.map(_safe, thunks))

    def _agent_qa(self, agent, question, model_fn):
        """One agent's read-only answer about its own recent work — a SINGLE bounded model
        call over its run digest, no tools/session/vault. Best-effort; never raises."""
        if model_fn is None:
            return "I can't answer right now."
        user = (f"Your recent activity:\n{self._agent_run_context(agent)}\n\n"
                f"Their message: {question}")
        try:
            return (model_fn(agent_config.GROUP_QA_SYSTEM, user) or "").strip() \
                or "Nothing to report."
        except Exception:                                  # noqa: BLE001
            return "I couldn't answer just now."

    def group_broadcast(self, group, text, owner):
        """Read-only group broadcast: every member agent answers the question grounded in
        its own recent run data. One bounded model call per member — NO tools, NO session,
        so it structurally cannot act. Returns [{agent_id, name, color, reply}].

        Members answer CONCURRENTLY. Asking three agents used to cost three model calls
        back to back, so the group waited on the sum; now it waits on the slowest one.
        ``_agent_qa`` never raises (it returns a fallback string), so a slow or failing
        member degrades to its own reply instead of taking the broadcast down.
        """
        root = self.sessions_root
        try:
            model_fn = self._summarize_model_fn()
        except Exception:                                  # noqa: BLE001 — no key in dev
            model_fn = None
        by_id = {a.get("agent_id"): a for a in registry.agents_for_user(root, owner)}
        members = [(aid, by_id[aid])
                   for aid in group.get("member_agent_ids", [])[:MAX_BROADCAST_MEMBERS]
                   if aid in by_id]                        # bound fan-out
        if not members:
            return []
        replies = self._fanout([lambda a=a: self._agent_qa(a, text, model_fn)
                                for _aid, a in members])
        return [{"agent_id": aid, "name": a.get("name") or aid,
                 "color": registry.agent_color(a),
                 "reply": reply if reply is not None else "I couldn't answer just now."}
                for (aid, a), reply in zip(members, replies)]

    @staticmethod
    def parse_mentions(text, members):
        """Agent ids @-mentioned in ``text``. Matches a member by its name, its no-space
        form, or its first word (case-insensitive): @Operations, @invoicing, @ops-bot."""
        low = (text or "").lower()
        hit = []
        for m in members:
            name = (m.get("name") or "").lower().strip()
            handles = {name, name.replace(" ", ""), name.split(" ")[0] if name else ""}
            handles.discard("")
            if any(("@" + h) in low for h in handles):
                hit.append(m.get("agent_id"))
        return hit

    def group_direct(self, agent, text, owner):
        """@mention in a group: run the agent on the user's message in a REAL session (so it
        CAN act), but respect staging — outbound actions in staged apps are PROPOSED for
        approval, not performed. Returns {agent_id, name, color, reply, staged}. Never raises."""
        aid = agent.get("agent_id")
        base = {"agent_id": aid, "name": agent.get("name") or aid,
                "color": registry.agent_color(agent), "staged": []}
        self._refresh_agent_binding(agent)
        staged_apps = [s for s in (agent.get("staged_apps") or []) if s]
        directive = agent_config.GROUP_DIRECT_DIRECTIVE.replace("{text}", text)
        trigger = agent_runner._staging_trigger(directive, staged_apps) if staged_apps else directive
        try:
            client = self._live_client()
            env = client.create_environment()
            sid = client.create_session(
                aid, env, resources=agent_runner._memory_resources(agent),
                title="Trace group message",
                vault_ids=[agent["vault_ref"]] if agent.get("vault_ref") else None)
            events = client.send_trigger_and_collect(sid, trigger)
        except Exception:                                  # noqa: BLE001
            return {**base, "reply": "I couldn't respond just now — try again in a moment."}
        human = agent_runner._final_text(events).split("```")[0].strip()   # drop any proposal fence
        reply = agent_runner._first_line(human) or "Done."
        staged = self._session_proposals(agent, sid, events, owner) if staged_apps else []
        return {**base, "reply": reply, "staged": staged}

    @staticmethod
    def run_key(agent_id, run):
        """The stable review key for one run: its session id, or a synthetic key for
        runs that never got a session (they can still be reviewed away)."""
        return run.get("run_session_id") or f"{agent_id}:{run.get('ran_at')}"

    def home_data(self, user):
        """Everything the agents home needs in ONE fast read (no model calls, no
        platform calls): the user's agents, each with its STORIES — the sessions the
        owner has NOT yet reviewed (or whose staged proposals are still pending),
        oldest first, with the pending proposals inline so they can be approved from
        the review view — plus the agent's bio and unread DM counts. Reviewing a
        story (POST .../reviews) moves that session into the profile's history log."""
        root = self.sessions_root
        uid = user["user_id"]
        unread = registry.messages_unread_counts(root, uid)
        pending_by_session = {}
        for p in registry.pending_for_user(root, uid):
            pending_by_session.setdefault(p.get("run_session_id"), []).append(
                {"id": p.get("id"), "app": p.get("app"), "summary": p.get("summary"),
                 "payload": p.get("payload") or {}})
        decisions_by_session = {}                           # Gap #3: unanswered questions
        for d in registry.decisions_for_user(root, uid, status="pending"):
            ctx = d.get("context") or {}
            decisions_by_session.setdefault(d.get("run_session_id"), []).append(
                {"id": d.get("id"), "question": d.get("question"),
                 "context": ctx.get("context") if isinstance(ctx, dict) else "",
                 "options": ctx.get("options") if isinstance(ctx, dict) else []})
        confirms_by_agent = {}                              # Phase B: shared lessons to confirm
        for p in registry.lesson_promotions_for_user(root, uid, status="provisional"):
            confirms_by_agent.setdefault(p.get("agent_id"), []).append(
                {"decision_id": p.get("decision_id"), "lesson": p.get("lesson")})
        active_by_agent = {}                                # Phase B: confirmed lessons (retirable)
        for p in registry.lesson_promotions_for_user(root, uid, status="confirmed"):
            active_by_agent.setdefault(p.get("agent_id"), []).append(
                {"decision_id": p.get("decision_id"), "lesson": p.get("lesson")})
        agents_out = []
        all_agents = registry.agents_for_user(root, uid)
        # Batched: one query each instead of three per agent (the home screen used to
        # cost 3N round trips and got slower with every agent deployed).
        agent_ids = [a.get("agent_id") for a in all_agents]
        reviewed_by_agent = registry.reviewed_keys_many(root, agent_ids)
        runs_by_agent = registry.runs_for_agents(root, agent_ids, 20)
        schedules_by_agent = registry.schedules_for_agents(root, agent_ids)
        for a in all_agents:
            aid = a.get("agent_id")
            reviewed = reviewed_by_agent.get(aid) or set()
            stories = []
            for r in runs_by_agent.get(aid) or []:
                key = self.run_key(aid, r)
                sid = r.get("run_session_id")
                props = pending_by_session.get(sid, []) if sid else []
                decs = decisions_by_session.get(sid, []) if sid else []
                if key in reviewed and not props and not decs:   # reviewed AND fully processed
                    continue
                note = None
                if sid:
                    note = (registry.summary_get(root, sid) or {}).get("summary")
                stories.append({"key": key, "ran_at": r.get("ran_at"),
                                "status": r.get("status"),
                                "note": note or r.get("summary") or "Ran.",
                                "proposals": props, "decisions": decs})
            stories.reverse()                          # review oldest first
            # Gap #4: split the open work into ACTIONABLE (needs a decision) vs FYI.
            open_decisions = sum(len(s["decisions"]) for s in stories)   # blocking questions
            pending_approvals = sum(len(s["proposals"]) for s in stories)  # awaiting a yes
            lesson_confirmations = confirms_by_agent.get(aid, [])         # Phase B: confirm to use
            needs_you = open_decisions + pending_approvals + len(lesson_confirmations)
            fyi_reviews = sum(1 for s in stories if not s["decisions"] and not s["proposals"])
            agents_out.append({
                "agent_id": aid, "name": a.get("name") or aid,
                "bundle_id": a.get("bundle_id"),
                "description": a.get("description") or "",
                "trigger": a.get("trigger") or "",
                "deployed_at": a.get("deployed_at"),
                "deployment": bool(a.get("deployment_id")),
                "schedule": schedules_by_agent.get(aid),
                "capture_present": bool(a.get("session_id")) and
                os.path.isdir(sessions.session_dir(root, a.get("session_id"))),
                "stories": stories, "unreviewed": len(stories),
                "open_decisions": open_decisions, "pending_approvals": pending_approvals,
                "lesson_confirmations": lesson_confirmations,
                "active_lessons": active_by_agent.get(aid, []),
                "needs_you": needs_you, "fyi_reviews": fyi_reviews,
                "unread": unread.get(aid, 0),
                "replying": (self.chat_jobs.get(aid) or {}).get("state") == "running"})
        return {"agents": agents_out, "unread_total": sum(unread.values()),
                "needs_you_total": sum(a["needs_you"] for a in agents_out)}

    def apply_platform_schedule(self, agent, cadence):
        """Re-point a deployment agent at the requested cadence. Deployments are
        immutable, so this creates a fresh one and archives the old — cron for
        daily/weekly (config.cadence_to_cron), or a manual deployment when ``cadence``
        is None/unmappable so Run-now keeps working. Raises on platform rejection (the
        caller surfaces the real error rather than phantom-succeeding).

        Ordering matters: CREATE the new deployment first, PERSIST it, and only THEN
        archive the old one. If the create fails, the old (working) deployment stays
        live and still registered — a failed schedule change never strands the agent
        with an archived deployment. Serialized per-process so two concurrent schedule
        POSTs can't each create a live cron deployment and orphan one."""
        with self.lock:
            client = self._live_client()
            old_id = agent.get("deployment_id")
            aid = agent.get("agent_id")
            # re-read the row so a racing change's deployment_id is the one we archive
            current = next((a for a in registry.agents_for_user(
                root=self.sessions_root, owner_user_id=agent.get("owner_user_id"))
                if a.get("agent_id") == aid), agent)
            old_id = current.get("deployment_id") or old_id
            trigger_text = agent_deploy.build_deployment_trigger(
                agent.get("trigger"), [s for s in (agent.get("staged_apps") or []) if s])
            store_id = agent.get("memory_store_id")
            new_id = client.create_deployment(       # create BEFORE archive
                name=f"trace-{agent.get('bundle_id') or aid}",
                agent_id=aid,
                environment_id=client.create_environment(),
                trigger_text=trigger_text,
                schedule=agent_config.cadence_to_cron(cadence) if cadence else None,
                resources=[agent_config.memory_resource(store_id)] if store_id else None,
                vault_ids=[agent.get("vault_ref")] if agent.get("vault_ref") else None,
                metadata={"trace_bundle_id": str(agent.get("bundle_id") or "")})
            registry.agent_set_deployment(self.sessions_root, aid, new_id)   # register first
            if old_id and old_id != new_id:
                client.archive_deployment(old_id)    # then retire the old one (tolerant)
            return new_id

    def _rejected_for(self, agent, limit=20):
        """Recently rejected proposals for this agent, newest first, so the next run can
        be told not to propose them again. Best-effort — never blocks a run.

        Rejecting used to set the row's status and stop there, which the agent never saw,
        so the identical action came back next run and the rejection taught nothing.
        """
        try:
            rows = registry.pending_for_user(self.sessions_root,
                                             agent.get("owner_user_id"), status="rejected")
        except Exception:                              # noqa: BLE001 — best-effort
            return []
        aid = agent.get("agent_id")
        return [{"summary": r.get("summary"), "app": r.get("app")}
                for r in rows if r.get("agent_id") == aid][:limit]

    def _refresh_agent_binding(self, agent):
        """Re-mint + rotate the Pipedream token in the agent's vault before a
        scheduled/Run-now run, so a run long after deploy still authenticates (the
        Pipedream dev token is ~1h; deploy refreshes via _refresh_stale_bindings, this
        is its equivalent on the scheduler path). Durable: uses only the vault_ref on
        the agent row, so it works even after a restart wiped the session's
        connection.json. Best-effort — a refresh failure lets the run proceed and
        surface its own auth error rather than silently skipping the run."""
        vault_ref = agent.get("vault_ref")
        if not vault_ref:
            return
        try:
            self.connect_fn().refresh_vault(vault_ref)
        except Exception:
            pass

    def run_agent(self, agent, resume=None):
        """Run one agent once (scheduled or "Run now"), persist the run record, and
        advance its last_run. Reuses the Deployer's run path via agent_runner; a
        missing key / platform error is recorded as a failed run, never raised.
        ``resume`` (Gap #3) seeds the run with a prior paused question + human answer."""
        self._refresh_agent_binding(agent)   # WS5: fresh Pipedream token before the run
        guidance = self._retrieve_guidance(agent)   # Gap #3 B4: lessons from past decisions
        rejected = self._rejected_for(agent)        # don't re-propose what was turned down
        try:
            client = self._live_client()
            rec = agent_runner.run_once(client, agent, resume=resume, guidance=guidance,
                                        rejected=rejected)
        except Exception:
            rec = {"agent_id": agent.get("agent_id"), "status": "failed",
                   "summary": "Couldn't start the run.", "details": [], "run_session_id": None}
        registry.run_add(self.sessions_root, {
            "agent_id": agent.get("agent_id"), "owner_user_id": agent.get("owner_user_id"),
            "status": rec["status"], "summary": rec["summary"],
            "details": rec.get("details") or [], "run_session_id": rec.get("run_session_id")})
        # Spec 8: persist any actions the agent staged for approval (deduped, so a
        # repeated run never re-stages the same proposal). Guarded so a persistence
        # error can't crash the run thread or flip the already-recorded outcome.
        for item in rec.get("pending") or []:
            try:
                registry.pending_add(self.sessions_root, {
                    "agent_id": agent.get("agent_id"), "owner_user_id": agent.get("owner_user_id"),
                    "run_session_id": item.get("run_session_id"), "app": item.get("app"),
                    "kind": item.get("kind"), "summary": item.get("summary"),
                    "payload": item.get("payload"), "content_hash": item.get("content_hash")})
            except Exception:
                pass
        registry.schedule_mark_run(self.sessions_root, agent.get("agent_id"))
        # Actionable outcomes arrive as a DM from the agent (routine completions
        # surface as stories instead, so the inbox stays signal-only).
        if rec["status"] == "failed":
            self._agent_dm(agent, f"My last run hit a problem: {rec['summary']}")
        elif rec["status"] == "needs_input":
            # Gap #3: the agent paused on a blocking edge case — persist the question(s)
            # as answerable decisions, then surface them.
            self._persist_decisions(agent, rec.get("run_session_id"),
                                    rec.get("questions"), agent.get("owner_user_id"))
            qs = rec.get("questions") or []
            first = (qs[0].get("question") if qs and isinstance(qs[0], dict) else "") \
                or "something I wasn't sure about"
            more = f" (+{len(qs) - 1} more)" if len(qs) > 1 else ""
            self._agent_dm(agent, f"I hit something I need your call on: {first}{more} — "
                                  "open my profile to answer.")
        else:
            # Staged agents propose rather than perform: extract this run's proposals
            # NOW (one session read) so they land in the feed + an approval DM without
            # waiting for the user to open the activity view.
            staged_props = []
            if agent.get("staged_apps") and rec.get("run_session_id"):
                try:
                    events = self._live_client().list_session_events(rec["run_session_id"])
                    staged_props = self._session_proposals(
                        agent, rec["run_session_id"], events, agent.get("owner_user_id"))
                except Exception:                          # noqa: BLE001 — view-time still covers it
                    staged_props = []
            n = (len(staged_props) or len(rec.get("pending") or [])
                 or (1 if rec["status"] == "needs_approval" else 0))
            if n:
                self._agent_dm(agent, f"I've staged {n} action{'s' if n != 1 else ''} for "
                                      "your approval — open my profile to review them.")
        return rec

    def approve_pending(self, item):
        """Spec 8: perform a staged action on the user's approval (the "quick fresh
        task"), then mark it processed. Returns the execution record. Never raises."""
        agent = self._agent_row(item.get("agent_id"), item.get("owner_user_id"))
        if not agent:
            registry.pending_set_status(self.sessions_root, item.get("id"), "failed")
            return {"status": "failed", "summary": "The agent is no longer available."}
        self._refresh_agent_binding(agent)
        try:
            client = self._live_client()
            res = agent_runner.execute_pending(client, agent, item)
        except Exception:
            res = {"status": "failed", "summary": "Couldn't perform it.", "run_session_id": None}
        registry.pending_set_status(self.sessions_root, item.get("id"),
                                    "executed" if res["status"] == "executed" else "failed")
        registry.run_add(self.sessions_root, {
            "agent_id": agent.get("agent_id"), "owner_user_id": agent.get("owner_user_id"),
            "status": "completed" if res["status"] == "executed" else "failed",
            "summary": res.get("summary") or "", "details": [item.get("summary") or item.get("kind") or "action"],
            "run_session_id": res.get("run_session_id")})
        return res

    def _agent_row(self, agent_id, owner_user_id):
        """The durable registry row for one of this user's deployed agents, or None."""
        if not agent_id or not owner_user_id:
            return None
        for a in registry.agents_for_user(self.sessions_root, owner_user_id):
            if a.get("agent_id") == agent_id:
                return a
        return None


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

        def _text(self, code, body, ctype, no_cache=False):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # The desktop app proxies the UI from the cloud so it auto-updates on
            # deploy; tell the webview never to cache UI assets, or it serves a
            # stale app.js after a redeploy and the new screen never appears.
            if no_cache:
                self.send_header("Cache-Control", "no-store, must-revalidate")
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
            for h in ("Cookie", "Content-Type", "Accept", "X-Admin-Token"):
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
                # Persist the reverse link so "delete capture data" can find this local
                # copy even after a desktop restart (the in-memory map is lost on restart).
                try:
                    sessions.update_meta(root, local_sid, cloud_sid=cloud_sid)
                except Exception:
                    pass
            return cloud_sid

        def _delete_capture_desktop(self):
            """Desktop side of "delete capture data": forward the delete to the cloud
            (removes the cloud copy + capture-derived platform data), then remove the
            matching LOCAL session dir. The cloud result is authoritative; the local
            removal is best-effort, located via the persisted ``cloud_sid`` meta link."""
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
            fwd = {}
            for h in ("Cookie", "Content-Type", "Accept", "X-Admin-Token"):
                if self.headers.get(h):
                    fwd[h] = self.headers.get(h)
            try:
                status, headers, data = state.proxy_fn(
                    state.cloud_backend, "POST", self.path, fwd, body)
            except Exception as e:                          # cloud unreachable
                return self._json(502, {"error": "cloud_unreachable", "detail": str(e)})
            if status == 200:
                try:
                    cloud_sid = json.loads(data).get("session_id")
                except (json.JSONDecodeError, AttributeError):
                    cloud_sid = None
                self._delete_local_for_cloud_sid(cloud_sid)
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _delete_local_for_cloud_sid(self, cloud_sid):
            """Remove the local capture session that was uploaded as ``cloud_sid`` (best
            effort): the durable ``meta.cloud_sid`` link, else the in-memory map."""
            if not cloud_sid:
                return
            local_sid = None
            for s in sessions.list_sessions(root):
                if s.get("id") and s.get("cloud_sid") == cloud_sid:
                    local_sid = s.get("id")
                    break
            if not local_sid:
                local_sid = next((l for l, c in state.local_to_cloud_sid.items() if c == cloud_sid), None)
            if local_sid:
                shutil.rmtree(sessions.session_dir(root, local_sid), ignore_errors=True)
                state.local_to_cloud_sid.pop(local_sid, None)

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
                u, token = users.create_user((body or {}).get("email"), (body or {}).get("password"))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json_auth(200, {"user": u}, set_token=users.start_session(u["user_id"], token))

        def _auth_login(self, body):
            u, token = users.verify((body or {}).get("email"), (body or {}).get("password"))
            if not u:
                return self._json(401, {"error": "invalid_credentials"})
            return self._json_auth(200, {"user": u}, set_token=users.start_session(u["user_id"], token))

        def _auth_logout(self):
            users.end_session(self._cookie(users.COOKIE_NAME))
            return self._json_auth(200, {"ok": True}, clear=True)

        def _owns(self, sid, user):
            return (sessions.read_meta(root, sid) or {}).get("owner_user_id") == user["user_id"]

        def _agents_for(self, user):
            """The logged-in user's deployed agents (across all their sessions), each
            with its schedule and recent run history — backs the agents dashboard
            (Spec 2) and the activity view (Spec 3). Scoped by owner; never another
            user's agents."""
            out = []
            agents = registry.agents_for_user(root, user["user_id"])
            ids = [a.get("agent_id") for a in agents]
            schedules = registry.schedules_for_agents(root, ids)     # batched: was 2 per agent
            runs = registry.runs_for_agents(root, ids, 5)
            for a in agents:
                aid = a.get("agent_id")
                out.append({**a,
                            "schedule": schedules.get(aid),
                            "recent_runs": runs.get(aid) or [],
                            "capture_present": bool(a.get("session_id")) and
                            os.path.isdir(sessions.session_dir(root, a.get("session_id")))})
            return out

        def _agent_action(self, agent_id, action, body, user):
            """Spec 4: set/clear/pause an agent's schedule, or run it now. Scoped to
            the logged-in user (only their own agents). Run-now is async (a real run
            can take minutes) — the dashboard polls /api/u/agents for the outcome."""
            agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                          if a.get("agent_id") == agent_id), None)
            if not agent:
                return self._json(404, {"error": "not_found"})
            if action == "schedule":
                cadence = (body or {}).get("cadence")
                if cadence is None:                       # null cadence = turn scheduling off
                    registry.schedule_clear(root, agent_id)
                else:
                    registry.schedule_set(root, agent_id, user["user_id"], cadence, paused=False)
                # Deployment agents: the platform schedules them — re-point the
                # deployment at the new cadence (archive + create; deployments are
                # immutable). A platform rejection is surfaced, never phantom-succeeded.
                if agent.get("deployment_id"):
                    try:
                        state.apply_platform_schedule(agent, cadence)
                    except Exception as e:                # noqa: BLE001
                        return self._json(502, {"error": "platform_schedule_failed",
                                                "detail": f"{type(e).__name__}: {e}"})
                return self._json(200, {"schedule": registry.schedule_get(root, agent_id)})
            if action == "schedule/pause":
                s = registry.schedule_get(root, agent_id)
                if not s:
                    return self._json(404, {"error": "no_schedule"})
                paused = bool((body or {}).get("paused", True))
                registry.schedule_set(root, agent_id, user["user_id"], s["cadence"],
                                      paused=paused)
                if agent.get("deployment_id"):
                    try:
                        client = state._live_client()
                        if paused:
                            client.pause_deployment(agent["deployment_id"])
                        else:
                            client.unpause_deployment(agent["deployment_id"])
                    except Exception as e:                # noqa: BLE001
                        return self._json(502, {"error": "platform_schedule_failed",
                                                "detail": f"{type(e).__name__}: {e}"})
                return self._json(200, {"schedule": registry.schedule_get(root, agent_id)})
            if action == "run-now":
                if agent.get("deployment_id"):
                    threading.Thread(target=lambda: state.run_deployment_now(agent),
                                     daemon=True).start()
                else:                                     # legacy (pre-deployment) agent
                    threading.Thread(target=lambda: state.run_agent(agent), daemon=True).start()
                return self._json(202, {"state": "running"})
            return self._json(404, {"error": "not_found"})

        def _pending_action(self, pending_id, action, user):
            """Spec 8: approve (Trace performs the staged action) or reject (discard) one
            pending item. Scoped to the owner. Approve runs async (a real connector call
            can take time); the dashboard polls /api/u/pending for the outcome."""
            item = registry.pending_get(root, pending_id)
            if not item or item.get("owner_user_id") != user["user_id"]:
                return self._json(404, {"error": "not_found"})
            if item.get("status") != "pending":
                return self._json(409, {"error": "already_processed"})
            if action == "reject":
                registry.pending_set_status(root, pending_id, "rejected")
                return self._json(200, {"id": pending_id, "status": "rejected"})
            threading.Thread(target=lambda: state.approve_pending(item), daemon=True).start()
            return self._json(202, {"id": pending_id, "state": "running"})

        def _set_staging(self, agent_id, body, user):
            """Spec 8: edit which of the agent's apps are staged (held for approval).
            Owner-scoped. Two shapes: ``{staged_apps: [...]}`` replaces the whole set
            (profile toggle), or ``{unstage: "<slug>"}`` drops one app (the per-proposal
            "auto-approve this app" quick action) computed server-side so it's race-free.
            The posted set is validated against the agent's usable apps; an empty result is
            allowed (fully autonomous — the UI confirms first). Takes effect next run."""
            agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                          if a.get("agent_id") == agent_id), None)
            if not agent:
                return self._json(404, {"error": "not_found"})
            b = body or {}
            if b.get("unstage"):                           # drop one app from the current set
                current = [s for s in (agent.get("staged_apps") or []) if s]
                staged = [s for s in current if s != b.get("unstage")]
            else:                                          # replace the whole set
                requested = [s for s in (b.get("staged_apps") or []) if s]
                available = {a["slug"] for a in state.agent_available_apps(agent)}
                # keep only verifiable slugs; if the app list can't be loaded, trust the request
                staged = [s for s in requested if s in available] if available else requested
            registry.set_staged_apps(root, agent_id, staged)
            return self._json(200, {"agent_id": agent_id, "staged_apps": staged})

        def _answer_decision(self, decision_id, body, user):
            """Gap #3: record the human's answer to a paused question, then auto-immediately
            resume the agent (fresh run seeded with Q+A) so it continues where it stopped.
            Owner-scoped; the resume runs async like approve."""
            item = registry.decision_get(root, decision_id)
            if not item or item.get("owner_user_id") != user["user_id"]:
                return self._json(404, {"error": "not_found"})
            if item.get("status") != "pending":
                return self._json(409, {"error": "already_answered"})
            answer = str((body or {}).get("answer", "") or "").strip()
            if not answer:
                return self._json(400, {"error": "empty_answer"})
            registry.decision_answer(root, decision_id, answer)
            agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                          if a.get("agent_id") == item.get("agent_id")), None)
            if agent:
                threading.Thread(
                    target=lambda: state.resume_after_answer(agent, item, answer),
                    daemon=True).start()
            return self._json(202, {"id": decision_id, "status": "answered", "state": "running"})

        def _confirm_lesson(self, body, user):
            """Phase B: confirm (promote), or dismiss/retire (revoke) a shared lesson for one
            agent. Owner-scoped. A confirmed lesson injects on later runs; dismiss (a
            provisional one) or retire (an already-confirmed one) both stop it and it never
            resurfaces for this agent."""
            b = body or {}
            agent_id, decision_id = str(b.get("agent_id") or ""), str(b.get("decision_id") or "")
            action = b.get("action")
            agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                          if a.get("agent_id") == agent_id), None)
            if not agent:
                return self._json(404, {"error": "not_found"})
            if not decision_id or action not in ("confirm", "dismiss", "retire"):
                return self._json(400, {"error": "bad_request"})
            promo = registry.lesson_promotion_get(root, agent_id, decision_id)
            if not promo:
                return self._json(404, {"error": "not_found"})
            new_status = "confirmed" if action == "confirm" else "retired"
            registry.lesson_promotion_set_status(root, agent_id, decision_id, new_status)
            return self._json(200, {"agent_id": agent_id, "decision_id": decision_id,
                                    "status": new_status})

        def _set_agent_consent(self, agent_id, body, user):
            """Phase B: set/clear an agent's per-field sharing override (owner-scoped).
            Returns the agent's effective scopes after the change."""
            agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                          if a.get("agent_id") == agent_id), None)
            if not agent:
                return self._json(404, {"error": "not_found"})
            b = body or {}
            registry.agent_set_consent(root, agent_id,
                contribute_scope=b.get("contribute_scope"), consume_scope=b.get("consume_scope"))
            fresh = next((a for a in registry.agents_for_user(root, user["user_id"])
                          if a.get("agent_id") == agent_id), agent)
            eff = registry.agent_lesson_scopes(root, fresh)
            return self._json(200, {"agent_id": agent_id, "effective": eff,
                "override": {"contribute_scope": fresh.get("lesson_contribute_scope"),
                             "consume_scope": fresh.get("lesson_consume_scope")}})

        # --- group chat (color-coded groups of agents) -------------------------
        def _owned_agent_ids(self, user):
            return {a.get("agent_id") for a in registry.agents_for_user(root, user["user_id"])}

        def _groups_with_members(self, user):
            """The user's groups, each member resolved to {agent_id, name, color} so the UI
            can render color bubbles. Stale member ids (deleted agents) are dropped."""
            by_id = {a.get("agent_id"): a for a in registry.agents_for_user(root, user["user_id"])}
            out = []
            for g in registry.groups_for_user(root, user["user_id"]):
                members = [{"agent_id": aid, "name": by_id[aid].get("name") or aid,
                            "color": registry.agent_color(by_id[aid])}
                           for aid in g.get("member_agent_ids", []) if aid in by_id]
                out.append({"id": g["id"], "name": g.get("name"), "members": members})
            return out

        def _create_group(self, body, user):
            b = body or {}
            name = str(b.get("name", "") or "").strip()
            if not name:
                return self._json(400, {"error": "empty_name"})
            owned = self._owned_agent_ids(user)
            ids = [a for a in (b.get("agent_ids") or []) if a in owned]   # only your agents
            registry.group_create(root, user["user_id"], name, ids)
            return self._json(200, {"groups": self._groups_with_members(user)})

        def _update_group(self, group_id, body, user):
            g = registry.group_get(root, group_id)
            if not g or g.get("owner_user_id") != user["user_id"]:
                return self._json(404, {"error": "not_found"})
            b = body or {}
            name = b.get("name")
            ids = None
            if b.get("agent_ids") is not None:
                owned = self._owned_agent_ids(user)
                ids = [a for a in b.get("agent_ids") if a in owned]
            registry.group_update(root, group_id,
                                  name=(str(name).strip() if name is not None else None),
                                  agent_ids=ids)
            return self._json(200, {"groups": self._groups_with_members(user)})

        def _delete_group(self, group_id, user):
            g = registry.group_get(root, group_id)
            if not g or g.get("owner_user_id") != user["user_id"]:
                return self._json(404, {"error": "not_found"})
            registry.group_delete(root, group_id)
            return self._json(200, {"groups": self._groups_with_members(user)})

        def _group_broadcast(self, group_id, body, user):
            """Read-only broadcast to a group: every member answers, grounded in its own run
            data, in its color. Owner-scoped; synchronous (bounded model calls)."""
            g = registry.group_get(root, group_id)
            if not g or g.get("owner_user_id") != user["user_id"]:
                return self._json(404, {"error": "not_found"})
            text = str((body or {}).get("text", "") or "").strip()
            if not text:
                return self._json(400, {"error": "empty_message"})
            if not state.msg_rate_ok(user["user_id"]):           # credit guard
                return self._json(429, {"error": "rate_limited",
                                        "message": "Too many messages — give it a moment."})
            return self._json(200, {"replies": state.group_broadcast(g, text, user["user_id"])})

        def _group_message(self, group_id, body, user):
            """Unified group send: @mention → the mentioned agents' actionable direct replies
            (real session, staging respected); otherwise a read-only broadcast to all."""
            g = registry.group_get(root, group_id)
            if not g or g.get("owner_user_id") != user["user_id"]:
                return self._json(404, {"error": "not_found"})
            text = str((body or {}).get("text", "") or "").strip()
            if not text:
                return self._json(400, {"error": "empty_message"})
            if not state.msg_rate_ok(user["user_id"]):           # credit guard
                return self._json(429, {"error": "rate_limited",
                                        "message": "Too many messages — give it a moment."})
            members = next((gg["members"] for gg in self._groups_with_members(user)
                            if gg["id"] == str(group_id)), [])
            mentioned = state.parse_mentions(text, members)[:MAX_MENTIONS_PER_MESSAGE]  # bound fan-out
            if mentioned:
                by_id = {a.get("agent_id"): a
                         for a in registry.agents_for_user(root, user["user_id"])}
                hits = [aid for aid in mentioned if aid in by_id]
                # Concurrent: each of these is a full platform session, so three
                # @mentions serially is three session round trips of waiting.
                replies = [r for r in state._fanout(
                    [lambda a=by_id[aid]: state.group_direct(a, text, user["user_id"])
                     for aid in hits]) if r is not None]
                return self._json(200, {"mode": "direct", "replies": replies})
            return self._json(200, {"mode": "broadcast",
                                    "replies": state.group_broadcast(g, text, user["user_id"])})

        def _set_agent_color(self, agent_id, body, user):
            if agent_id not in self._owned_agent_ids(user):
                return self._json(404, {"error": "not_found"})
            color = str((body or {}).get("color", "") or "").strip()
            if not color:
                return self._json(400, {"error": "empty_color"})
            registry.agent_set_color(root, agent_id, color)
            return self._json(200, {"agent_id": agent_id, "color": color})

        def _set_agent_name(self, agent_id, body, user):
            """Rename an agent. The deployed name on the platform is unchanged — this is
            the label the owner sees, and the memory store keys on the agent id, so a
            rename cannot orphan the agent's accumulated lessons."""
            if agent_id not in self._owned_agent_ids(user):
                return self._json(404, {"error": "not_found"})
            name = str((body or {}).get("name", "") or "").strip()
            if not name:
                return self._json(400, {"error": "empty_name"})
            if len(name) > 80:
                return self._json(400, {"error": "name_too_long"})
            registry.agent_set_name(root, agent_id, name)
            return self._json(200, {"agent_id": agent_id, "name": name})

        def _delete_capture_data(self, agent_id, user):
            """Delete one workflow's capture data (the origin session). Resolves the
            agent owner-scoped, then removes its capture session; the agent stays live."""
            agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                          if a.get("agent_id") == agent_id), None)
            if not agent:
                return self._json(404, {"error": "not_found"})
            sid = agent.get("session_id")
            if not sid:
                return self._json(404, {"error": "no_capture_session"})
            res = state.delete_capture_data(sid, user["user_id"])
            if res.get("error"):
                return self._json(404, res)
            return self._json(200, res)

        # --- end-user namespace (WS3): same pipeline, scoped to the logged-in user ---
        def _user_get(self, rest, user):
            if rest in ("/me", ""):
                return self._json(200, {"user": user, "signup_enabled": _signup_enabled()})
            if rest == "/sessions":
                mine = sessions.list_sessions(root, owner_id=user["user_id"])
                return self._json(200, {"sessions": mine})
            if rest == "/fixtures":                          # demo entry without mac capture
                return self._json(200, {"fixtures": [
                    {"name": f["name"]} for f in list_fixtures()]})
            if rest == "/preflight":
                return self._json(200, _preflight(state))
            if rest == "/agents":                            # Spec 2/3: all my agents + activity
                return self._json(200, {"agents": self._agents_for(user),
                                        "pending_count": registry.pending_count_for_user(root, user["user_id"])})
            if rest == "/home":                              # Instagram-style home: stories + feed + DMs
                return self._json(200, state.home_data(user))
            if rest == "/pending":                           # Spec 8: staged actions awaiting review
                items = registry.pending_for_user(root, user["user_id"])
                return self._json(200, {"pending": items, "pending_count": len(items)})
            if rest == "/consent":                           # Phase B: account-wide lesson sharing
                return self._json(200, registry.user_consent_get(root, user["user_id"]))
            if rest == "/groups":                            # group chat: list groups + members
                return self._json(200, {"groups": self._groups_with_members(user)})
            mdm = re.match(r"^/agents/([^/]+)/messages$", rest)
            if mdm:                                          # one DM thread, oldest-first
                agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                              if a.get("agent_id") == mdm.group(1)), None)
                if not agent:
                    return self._json(404, {"error": "not_found"})
                aid = agent["agent_id"]
                return self._json(200, {
                    "messages": registry.messages_for_agent(root, user["user_id"], aid),
                    "replying": (state.chat_jobs.get(aid) or {}).get("state") == "running",
                    "agent": {"agent_id": aid, "name": agent.get("name") or aid}})
            maa = re.match(r"^/agents/([^/]+)/activity$", rest)
            if maa:                                          # real session activity (Haiku summaries)
                agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                              if a.get("agent_id") == maa.group(1)), None)
                if not agent:
                    return self._json(404, {"error": "not_found"})
                return self._json(200, {"agent": {"agent_id": agent.get("agent_id"),
                                                  "name": agent.get("name") or agent.get("bundle_id"),
                                                  "staged_apps": agent.get("staged_apps") or [],
                                                  "available_apps": state.agent_available_apps(agent),
                                                  "consent": {
                                                      "effective": registry.agent_lesson_scopes(root, agent),
                                                      "override": {
                                                          "contribute_scope": agent.get("lesson_contribute_scope"),
                                                          "consume_scope": agent.get("lesson_consume_scope")}}},
                                        "activity": state.agent_activity(agent)})
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
            ma = re.match(r"^/agents/([^/]+)/(schedule|schedule/pause|run-now)$", rest)
            if ma:                                       # Spec 4: schedule / run-now
                return self._agent_action(ma.group(1), ma.group(2), body, user)
            mrv = re.match(r"^/agents/([^/]+)/reviews$", rest)
            if mrv:                                      # mark one story (session) reviewed
                agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                              if a.get("agent_id") == mrv.group(1)), None)
                if not agent:
                    return self._json(404, {"error": "not_found"})
                key = str((body or {}).get("key", "") or "").strip()
                if not key:
                    return self._json(400, {"error": "missing_key"})
                registry.review_mark(root, agent["agent_id"], user["user_id"], key)
                return self._json(200, {"ok": True})
            mdm = re.match(r"^/agents/([^/]+)/messages(/read)?$", rest)
            if mdm:                                      # DMs: send to the agent / mark read
                agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                              if a.get("agent_id") == mdm.group(1)), None)
                if not agent:
                    return self._json(404, {"error": "not_found"})
                aid = agent["agent_id"]
                if mdm.group(2):                         # /read
                    registry.messages_mark_read(root, user["user_id"], aid)
                    return self._json(200, {"ok": True})
                text = str((body or {}).get("text", "") or "").strip()
                if not text:
                    return self._json(400, {"error": "empty_message"})
                if (state.chat_jobs.get(aid) or {}).get("state") == "running":
                    return self._json(409, {"error": "already_replying"})
                if not state.msg_rate_ok(user["user_id"]):   # credit guard (a DM = a full session)
                    return self._json(429, {"error": "rate_limited",
                                            "message": "Too many messages — give it a moment."})
                state.send_agent_message(agent, text)
                return self._json(202, {"state": "running"})
            mp = re.match(r"^/pending/([^/]+)/(approve|reject)$", rest)
            if mp:                                       # Spec 8: approve/reject a staged action
                return self._pending_action(mp.group(1), mp.group(2), user)
            md = re.match(r"^/agents/([^/]+)/delete-capture-data$", rest)
            if md:                                       # delete this workflow's capture data
                return self._delete_capture_data(md.group(1), user)
            maa = re.match(r"^/agents/([^/]+)/approve-actions$", rest)
            if maa:                                      # approve selected staged actions (activity screen)
                agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                              if a.get("agent_id") == maa.group(1)), None)
                if not agent:
                    return self._json(404, {"error": "not_found"})
                ids = (body or {}).get("pending_ids") or []
                threading.Thread(target=lambda: state.approve_actions(agent, ids), daemon=True).start()
                return self._json(202, {"state": "running"})
            mst = re.match(r"^/agents/([^/]+)/staging$", rest)
            if mst:                                      # Spec 8: edit which apps are staged (no redeploy)
                return self._set_staging(mst.group(1), body, user)
            man = re.match(r"^/decisions/([^/]+)/answer$", rest)
            if man:                                      # Gap #3: answer a paused question, auto-resume
                return self._answer_decision(man.group(1), body, user)
            if rest == "/lessons/confirm":               # Phase B: confirm/dismiss a shared lesson
                return self._confirm_lesson(body, user)
            if rest == "/consent":                       # Phase B: set account-wide sharing default
                registry.user_consent_set(root, user["user_id"],
                    contribute_scope=(body or {}).get("contribute_scope"),
                    consume_scope=(body or {}).get("consume_scope"))
                return self._json(200, registry.user_consent_get(root, user["user_id"]))
            mac = re.match(r"^/agents/([^/]+)/consent$", rest)
            if mac:                                      # Phase B: per-agent sharing override
                return self._set_agent_consent(mac.group(1), body, user)
            if rest == "/groups":                        # group chat: create a group
                return self._create_group(body, user)
            mgd = re.match(r"^/groups/([^/]+)/delete$", rest)
            if mgd:
                return self._delete_group(mgd.group(1), user)
            mgb = re.match(r"^/groups/([^/]+)/broadcast$", rest)
            if mgb:                                      # read-only group Q&A: every member replies
                return self._group_broadcast(mgb.group(1), body, user)
            mgm = re.match(r"^/groups/([^/]+)/message$", rest)
            if mgm:                                      # unified: @mention → direct, else broadcast
                return self._group_message(mgm.group(1), body, user)
            mg = re.match(r"^/groups/([^/]+)$", rest)
            if mg:                                       # rename / set members
                return self._update_group(mg.group(1), body, user)
            mcol = re.match(r"^/agents/([^/]+)/color$", rest)
            if mcol:                                     # set an agent's bubble color
                return self._set_agent_color(mcol.group(1), body, user)
            mnm = re.match(r"^/agents/([^/]+)/name$", rest)
            if mnm:                                      # rename an agent (display only)
                return self._set_agent_name(mnm.group(1), body, user)
            if rest == "/load-fixture":
                return self._load_fixture(body, owner=uid)
            if rest == "/describe-workflow":                 # skip-recording path
                return self._describe_workflow(body, owner=uid)
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
                                        "database": "supabase" if supabase_client.is_configured()
                                        else "local"})
            if p == "/" or p == "/index.html":
                return self._serve_static("index.html")
            if p.startswith("/static/"):
                return self._serve_static(p[len("/static/"):])
            if p == "/app" or p == "/app/":                 # end-user UI (WS2)
                return self._serve_app("index.html")
            if p.startswith("/app/"):
                return self._serve_app(p[len("/app/"):])
            if p == "/admin" or p == "/admin/":             # admin portal (data is token-gated)
                return self._serve_admin("index.html")
            if p.startswith("/admin/"):
                return self._serve_admin(p[len("/admin/"):])
            if p.startswith("/api/admin/"):                 # token-gated admin API (GET-only)
                if self._admin_gate():                       # True => blocked (403 already sent)
                    return
                rest = p[len("/api/admin"):]
                if rest == "/overview":
                    return self._admin_overview()
                if rest == "/accounts":
                    return self._admin_accounts()
                if rest == "/pipeline":
                    return self._admin_pipeline()
                if rest == "/prompts":
                    q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) \
                        if "?" in self.path else {}
                    return self._admin_prompts(q)
                if rest == "/architecture":
                    return self._admin_architecture()
                return self._json(404, {"error": "not_found"})
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
            # Desktop: "delete capture data" must wipe BOTH the cloud copy (proxied) and
            # the LOCAL copy — intercept before the generic proxy to do both.
            if state.cloud_backend and re.match(r"^/api/u/agents/[^/]+/delete-capture-data$", p):
                return self._delete_capture_desktop()
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
                self._text(200, f.read(), _CONTENT_TYPES.get(ext, "application/octet-stream"),
                           no_cache=True)

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
                self._text(200, f.read(), _CONTENT_TYPES.get(ext, "application/octet-stream"),
                           no_cache=True)

        def _serve_admin(self, rel):
            """Serve the admin portal from admin_static/ (same SPA fallback + no-store
            as the end-user UI). The PAGE is public; every /api/admin/* data call it
            makes is token-gated — so serving the shell leaks nothing."""
            rel = (rel or "").lstrip("/") or "index.html"
            full = os.path.normpath(os.path.join(ADMIN_STATIC_DIR, rel))
            if not full.startswith(ADMIN_STATIC_DIR) or not os.path.isfile(full):
                full = os.path.join(ADMIN_STATIC_DIR, "index.html")
            if not os.path.isfile(full):
                return self._json(404, {"error": "not_found"})
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as f:
                self._text(200, f.read(), _CONTENT_TYPES.get(ext, "application/octet-stream"),
                           no_cache=True)

        # --- admin portal API (token-gated; GET-only; never unauthenticated) ---
        def _admin_gate(self):
            """Sends a 403 and returns True when the request must be BLOCKED; returns
            False (nothing sent) when it may proceed. Fail closed: with TRACE_ADMIN_TOKEN
            unset the whole admin surface is disabled. Constant-time compare.
            (Returning a truthy sentinel — not the _json result, which is None — is what
            makes the caller's `if self._admin_gate(): return` actually block.)"""
            expected = (os.environ.get("TRACE_ADMIN_TOKEN") or "").strip()
            if not expected:
                self._json(403, {"error": "admin_disabled"})
                return True
            supplied = (self.headers.get("X-Admin-Token") or "").strip()
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                self._json(403, {"error": "forbidden"})
                return True
            return False

        def _admin_overview(self):
            sched = state._scheduler_thread
            return self._json(200, {
                "users": len(users.list_users()),
                "agents": len(registry.agents_all(root)),
                "sessions": len(sessions.list_sessions(root)),
                "db_mode": "supabase" if supabase_client.is_configured() else "files",
                "scheduler_alive": bool(sched and sched.is_alive()),
                "cloud_backend": state.cloud_backend,
                "signup_enabled": _signup_enabled(),
                "prompt_entries": len(state.prompt_log),
                "uptime_s": int(time.time() - state.started_at)})

        def _admin_accounts(self):
            """Every account with its sessions, agents (schedule + recent runs), and
            pending count — the whole platform at a glance."""
            all_sessions = sessions.list_sessions(root)
            all_agents = registry.agents_all(root)
            out = []
            for u in users.list_users():
                uid = u["user_id"]
                mine_sessions = [
                    {"id": s.get("id"), "label": s.get("label"),
                     "created_at": s.get("created_at"), "described": bool(s.get("described")),
                     "artifacts": {k: bool(v) for k, v in (s.get("artifacts") or {}).items()
                                   if k in ("raw", "event_log", "discovery", "reviewed", "specs")},
                     "deploy_at": s.get("deploy_at")}
                    for s in all_sessions if s.get("owner_user_id") == uid]
                mine_agents = []
                for a in all_agents:
                    if a.get("owner_user_id") != uid:
                        continue
                    aid = a.get("agent_id")
                    mine_agents.append({
                        "agent_id": aid, "name": a.get("name"),
                        "bundle_id": a.get("bundle_id"),
                        "deployed_at": a.get("deployed_at"),
                        "deployment_id": a.get("deployment_id"),
                        "memory_store_id": a.get("memory_store_id"),
                        "staged_apps": a.get("staged_apps") or [],
                        "schedule": registry.schedule_get(root, aid),
                        "recent_runs": registry.runs_for_agent(root, aid, 5)})
                out.append({"user": u, "sessions": mine_sessions, "agents": mine_agents,
                            "pending_count": registry.pending_count_for_user(root, uid)})
            return self._json(200, {"accounts": out})

        def _admin_pipeline(self):
            """Live snapshot of every in-process pipeline job, merged per session id —
            the real-time 'follow the agent creation process' feed."""
            stages = {"capture": None, "reconstruct": state.recon_jobs,
                      "discover": state.discover_jobs, "describe": state.describe_jobs,
                      "specs": state.spec_jobs, "interview": state.interview_jobs,
                      "build": state.build_jobs, "connect": state.connection_jobs,
                      "deploy": state.deploy_jobs}
            by_sid = {}
            for stage, jobs in stages.items():
                for sid, job in (jobs or {}).items():
                    by_sid.setdefault(sid, {})[stage] = job
            meta = {sid: {"owner_user_id": (sessions.read_meta(root, sid) or {}).get("owner_user_id"),
                          "label": (sessions.read_meta(root, sid) or {}).get("label")}
                    for sid in by_sid}
            return self._json(200, {"sessions": by_sid, "meta": meta,
                                    "capture": state.current_sid})

        def _admin_prompts(self, query):
            try:
                since = int((query.get("since") or ["0"])[0])
            except (TypeError, ValueError):
                since = 0
            with state.prompt_lock:
                entries = [e for e in state.prompt_log if e.get("seq", 0) > since]
                latest = state.prompt_seq
            return self._json(200, {"entries": entries, "latest_seq": latest})

        def _admin_architecture(self):
            """The system map, with live annotations so the diagram reflects THIS
            running instance. Static content otherwise — no secrets."""
            return self._json(200, {
                "components": [
                    {"id": "desktop", "label": "Desktop app (Tauri + trace-node sidecar)",
                     "detail": "Local capture; proxies auth + pipeline to the cloud",
                     "connects_to": ["cloud", "extension", "collector"]},
                    {"id": "extension", "label": "Chrome extension",
                     "detail": "Browser half of capture (clicks, targets, structured reads)",
                     "connects_to": ["collector"]},
                    {"id": "collector", "label": "Capture collector (127.0.0.1:8137)",
                     "detail": "Merges browser + local events into raw_events.jsonl (redacted)",
                     "connects_to": []},
                    {"id": "cloud", "label": "Cloud node (Render)",
                     "detail": "Pipeline engines, scheduler + deployment sync, this admin API",
                     "connects_to": ["postgres", "anthropic", "pipedream"],
                     "live": {"db_mode": "supabase" if supabase_client.is_configured() else "files",
                              "scheduler_alive": bool(state._scheduler_thread
                                                      and state._scheduler_thread.is_alive())}},
                    {"id": "postgres", "label": "Postgres (Render managed)",
                     "detail": "Accounts, login sessions, agents, schedules, runs, pending",
                     "connects_to": []},
                    {"id": "anthropic", "label": "Anthropic Managed Agents",
                     "detail": "Agents, deployments (manual/cron), sessions, memory stores, vaults",
                     "connects_to": ["pipedream"]},
                    {"id": "pipedream", "label": "Pipedream Connect (MCP)",
                     "detail": "OAuth to the customer's apps; per-customer MCP servers",
                     "connects_to": []},
                ],
                "pipeline": ["capture", "reconstruct", "discover | describe", "review",
                             "questions (mandatory)", "build", "connect", "deploy",
                             "runs + approvals + memory"]})

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
                sid = sessions.create_session(root, owner_id=owner)
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
                # Always clear state so the user can start a new session,
                # even if the stop/sync below throws.
                state.controller = None
                state.current_sid = None
            # Outside the lock: stop capture and sync (non-blocking for state).
            try:
                controller.stop()
                count = sessions.artifacts(root, sid)["raw_event_count"]
                health = controller.summary()
                sessions.update_meta(root, sid, stopped_at=sessions._now(),
                                     raw_event_count=count,
                                     source_summary=health["by_source"],
                                     capture_warning=health["message"])
                sessions.sync_to_supabase(root, sid,
                                          stopped_at=sessions._now(),
                                          status="stopped",
                                          event_count=count)
                meta = sessions.read_meta(root, sid)
                sessions._upload_to_storage(root, sid, sessions.RAW,
                                            meta.get("owner_user_id") or "")
            except Exception:
                import traceback; traceback.print_exc()
                count = count if 'count' in dir() else 0
                health = health if 'health' in dir() else {}
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
            if sub == "/describe/status":                    # skip-recording path
                return self._json(200, state.describe_jobs.get(sid) or {"state": "idle"})
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

        # --- describe (skip-recording path) ---
        def _describe_workflow(self, body, owner):
            """Create a session from a TYPED workflow description (no capture). The
            describe job then writes the same artifacts the capture path produces
            (event_log=[] + discovery.json), so review -> questions -> build -> connect
            -> deploy run unchanged."""
            description = str((body or {}).get("description", "") or "").strip()
            if not description:
                return self._json(400, {"error": "empty_description"})
            if not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            sid = sessions.create_session(root, label="described", owner_id=owner)
            sessions.update_meta(root, sid, owner_user_id=owner,
                                 described=True, description=description)
            with state.lock:
                state.start_describe(sid, description)
            return self._json(202, {"state": "running", "session_id": sid})

        # --- discovery (Stage 2) ---
        def _load_fixture(self, body, owner=None):
            name = (body or {}).get("name", "")
            fx = next((f for f in list_fixtures() if f["name"] == name), None)
            if not fx:
                return self._json(404, {"error": "unknown_fixture"})
            sid = sessions.create_session(root, label=f"fixture:{name}", owner_id=owner)
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
            sessions.write_json_artifact(root, sid, "scorecard", sc)
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
            # The approved set may have changed: clear interview_at so the UI
            # regenerates questions (prior answers whose qid still maps survive),
            # then resync — an unchanged, fully-answered set re-passes immediately.
            sessions.update_meta(root, sid, reviewed_at=sessions._now(), interview_at=None)
            sessions.write_json_artifact(root, sid, "reviewed_bundles", cleaned)
            state._sync_interview_complete(sid)
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
            # The questions phase is mandatory: no build until every approved
            # bundle's interview is complete (recomputed from disk, never the meta flag).
            if not state._interview_all_complete(sid):
                return self._json(400, {"error": "interview_incomplete"})
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
            start = {"trigger": b.get("trigger"), "schedule": b.get("schedule"),
                     "staged_apps": b.get("staged_apps")}   # Spec 8: apps staged for approval
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
    # Supabase schema is managed via migrations (001+002 already deployed).
    # No init_db() needed — Supabase handles this externally.
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

    httpd, state = build_server(host=args.host, port=args.port,
                                sessions_root=args.sessions_root,
                                collector_port=args.collector_port,
                                cloud_backend=args.cloud_backend)
    state.start_scheduler()   # Spec 4 (no-op on the desktop node; see start_scheduler)
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
