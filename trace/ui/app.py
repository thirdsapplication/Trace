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
import collections
import concurrent.futures
import hmac
import json
import os
import queue
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
from ..agent import gate as agent_gate
from ..agent import score as agent_score
from ..agent import deploy as agent_deploy
from ..agent import runner as agent_runner
from ..agent import activity as agent_activity
from ..agent import embeddings as agent_embeddings
from ..agent import generalize as agent_generalize
from ..agent import learn as agent_learn
from ..agent import peers as agent_peers
from ..agent import scrub as agent_scrub
from . import (cloud, keystore, notify, permissions, registry, sessions,
               supabase_client, teams, users)


class _Server(ThreadingHTTPServer):
    """The stdlib listen backlog is 5. Once a handful of handlers are blocked, new
    connections are never accepted — which presents as a request that hangs before
    any of our code runs, indistinguishable from a slow endpoint."""
    request_queue_size = 128
    daemon_threads = True
    allow_reuse_address = True


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

# The safety net (frame 10): when an agent should stop mid-run and come get you. Rides the
# SAME reserved-answer path as the note above, for the same reason — it reaches the agent
# config through configgen._load_answers with no plumbing of its own, and no new table.
# Set once for every approved workflow, then tightened per agent from its profile later.
SAFETY_RULES_ID = "safety_rules"
SAFETY_RULES_LABEL = "When should this agent stop and come get you?"


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
# Desktop: how long one UI asset may wait on the cloud before falling back to the
# bundled copy. Short on purpose — a cold backend must cost a beat, never the app.
DESKTOP_UI_TIMEOUT = 4
DESKTOP_UI_CACHE_TTL = 60        # seconds a fetched asset is reused in-process
# Phase B (B5d): a cross-user lesson confirmed by this many DISTINCT users graduates to a
# global auto-trusted default — it then injects without the per-agent confirm checkpoint.
FLEET_DEFAULT_CONFIRMATIONS = 5
# The activity view used to make platform AND model calls inside the page load — up
# to `limit` of each. Capping and parallelizing them was not enough: a page that does
# live network I/O has a floor of seconds. That work now runs in refresh_activity on
# a background thread and the view reads what it persisted, so this bound is a cost
# ceiling on ONE refresh rather than a deadline a user is waiting behind.
ACTIVITY_SUMMARY_BUDGET = 10         # summaries computed per background refresh
# Fleet-wide ceiling on concurrent refreshes. Each spawns up to ~21 threads and
# dozens of queries, and the page polls the very endpoint that starts them.
ACTIVITY_MAX_CONCURRENT = 2


def _why(e):
    """An exception as something a person can act on.

    Every pipeline job reported `str(e)`, which drops the TYPE — usually the
    informative half, since AuthenticationError and JSONDecodeError call for
    completely different responses — and is EMPTY for anything raised without a
    message, leaving the user a step that failed and a blank reason. This is the same
    shape `_build_one` already used; the other nine paths did not.
    """
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__



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
                 summarize_fn=None, learn_fn=None, learn_score_fn=None,
                 scrub_fn=None, scrub_verify_fn=None):
        self.sessions_root = sessions_root
        self.collector_port = collector_port
        # The founder Test UI (`/`, `/static/`, the unauthenticated `/api/*` routes)
        # is a loopback development tool. On a public bind it would expose every
        # session's raw capture and let anyone overwrite the server's keys, so
        # build_server() flips this off for any non-loopback host. True here so a
        # bare AppState (tests, embedding) keeps today's local behavior.
        self.test_ui_enabled = True
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
        # Learning cycles: a model_fn(system, user)->str that analyzes a batch of
        # sessions and rewrites lessons_learned.md (Fable). Stub in tests; built
        # lazily from the key.
        self.learn_fn = learn_fn
        # The DIFFICULTY scorer of the learning cycle (Sonnet), a different call from
        # the analyst above on purpose — see score.py. Named learn_score_fn and not
        # score_fn because ``score_fn`` is taken, by the discovery scorer, and the seam
        # that reached for it silently got that instead of a model caller: every cycle
        # in production called discovery_scorer.score(system, user), which raised, and
        # no cycle ever produced a measurement.
        self.learn_score_fn = learn_score_fn
        # Collective learning: the scrub pass and its independent privacy gate. Two
        # seams, not one, because the whole point of the gate is that it is a SECOND
        # opinion — a test that stubs both with the same callable is testing plumbing,
        # and should have to say so.
        self.scrub_fn = scrub_fn
        self.scrub_verify_fn = scrub_verify_fn
        self.controller_factory = controller_factory or (
            lambda out: CaptureController(out, collector_port=collector_port))
        self.keystore = key_store or keystore
        self.controller = None       # current CaptureController (one session at a time)
        self.current_sid = None
        # Load any stored Pipedream Connect settings into the agent config so every
        # consumer (ConnectClient defaults, config.pipedream_mcp_url at deploy, preflight)
        # sees them without env vars.
        #
        # On the desktop node this must NOT run before the HTTP port binds: keyring
        # reads hit the macOS Keychain, and an unsigned build can pop a modal prompt
        # that blocks until answered — the shell would sit on "Starting…" the whole
        # time. Every consumer of PIPEDREAM_* is proxied to the cloud in desktop
        # mode, so nothing local races the background load.
        if self.cloud_backend:
            threading.Thread(target=_apply_pipedream_settings,
                             args=(self.keystore,), daemon=True).start()
        else:
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
        self._learning_inflight = set()   # agent_ids with a learning cycle running
        self._publish_inflight = set()    # agent_ids publishing a peer bundle
        self._learning_checked = {}  # agent_id -> epoch of last learning trigger check
        # Session ids that 404'd (deleted/expired on the platform). A gone session
        # never comes back, so each is discovered once and then skipped by BOTH the
        # learning cycle and the activity refresh — without this, an agent whose
        # history was cleaned up re-fetched the same dead sessions forever.
        # Purpose-built locks. `self.lock` is held ACROSS network I/O by the
        # scheduler (_record_deployment_run) and by schedule changes
        # (apply_platform_schedule), so anything a page load touches must never
        # wait on it — that is how a database-only endpoint still hung behind a
        # 60-second scheduler pass.
        self._bg_lock = threading.Lock()      # background claim sets (set ops only)
        self._runs_lock = threading.Lock()    # run-row check-then-insert
        self._deploy_lock = threading.Lock()  # deployment create/archive per agent
        self._ui_cache = {}          # desktop: rel -> (ctype, bytes, expires_epoch)
        self._sessions_gone = set()
        self._activity_refreshing = set()  # agent_ids with a background refresh running
        self.chat_jobs = {}          # agent_id -> {state, error} (a DM reply in flight)
        self._approval_queues = {}   # agent_id -> queue.Queue of tasks (FIFO per agent)
        self._approval_workers = {}  # agent_id -> live drain thread
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

    def ui_cache_get(self, rel):
        """A recently fetched cloud UI asset, or None. Desktop only."""
        hit = self._ui_cache.get(rel)
        if not hit or hit[2] <= time.time():
            return None
        return hit[0], hit[1]

    def ui_cache_put(self, rel, ctype, data):
        self._ui_cache[rel] = (ctype, data, time.time() + DESKTOP_UI_CACHE_TTL)

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
        try:
            connect_client = self.connect_fn()    # enables the tools/list preflight
        except Exception:
            connect_client = None                 # preflight is optional, deploy isn't
        return agent_deploy.deploy_live(bundle, live_dict, connection_record, client,
                                        trigger=start.get("trigger"), schedule=start.get("schedule"),
                                        approval_required=start.get("approval_required"),
                                        connect_client=connect_client)

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
                job.update(state="error", error=_why(e))

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
                job.update(state="error", error=_why(e))

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
                job.update(state="error", error=_why(e))

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
                job.update(state="error", error=_why(e))

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
        # Bounded: this runs inside a user-facing GET, so a stuck call must fail fast
        # rather than hold the page (the default client waits ~10 min per attempt).
        return self._tap("activity", None, agent_config.SUMMARY_MODEL,
                         lambda: agent_configgen._default_model_fn(
                             agent_config.SUMMARY_MODEL,
                             timeout=agent_config.SUMMARY_REQUEST_TIMEOUT,
                             max_retries=agent_config.SUMMARY_RETRIES))

    def _learning_model_fn(self):
        """The injected learner if present (tests), else a lazy Fable caller from the
        saved key. Raises ``no_api_key`` only if it actually needs to call the model.
        Every call lands in the admin prompt log under engine "learning"."""
        if self.learn_fn:
            return self._tap("learning", None, agent_config.LEARNING_MODEL,
                             lambda: self.learn_fn)
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        return self._tap("learning", None, agent_config.LEARNING_MODEL,
                         lambda: agent_learn._default_model_fn(agent_config.LEARNING_MODEL))

    def _scrub_model_fn(self):
        """The injected scrubber if present (tests), else a lazy Sonnet caller from the
        saved key. Raises ``no_api_key`` only if it actually needs to call the model.
        Every call lands in the admin prompt log under engine "scrub" — the tap carries
        the same transcripts the learning tap already carries, and being able to read
        back exactly what the scrub was asked to rewrite is the only way a bundle that
        came out wrong can be diagnosed after the fact."""
        if self.scrub_fn:
            return self._tap("scrub", None, agent_scrub.DEFAULT_MODEL,
                             lambda: self.scrub_fn)
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        return self._tap("scrub", None, agent_scrub.DEFAULT_MODEL,
                         lambda: agent_scrub._default_model_fn(agent_scrub.DEFAULT_MODEL))

    def _scrub_verify_model_fn(self):
        """The independent privacy gate on a scrubbed bundle — a DIFFERENT (stronger)
        model than the one that did the scrubbing, because a model checking its own work
        is one opinion counted twice, and this is the last check before material crosses
        an account boundary. Logged under engine "scrub-verify"."""
        if self.scrub_verify_fn:
            return self._tap("scrub-verify", None, agent_scrub.VERIFY_MODEL,
                             lambda: self.scrub_verify_fn)
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        return self._tap("scrub-verify", None, agent_scrub.VERIFY_MODEL,
                         lambda: agent_scrub._default_verify_model_fn(
                             agent_scrub.VERIFY_MODEL))

    # --- teams (frame 30) -------------------------------------------------
    def team_view(self, user):
        """The whole Team tab in one read: the team, its members with their agent counts,
        and the invites nobody has accepted. No team yet is a normal state, not an error —
        one is created by the first invite."""
        root, uid = self.sessions_root, user["user_id"]
        t = teams.team_for_user(root, uid)
        # Invites addressed to THIS user, as opposed to the ones their team has sent.
        # Without these the pane can only ever show a team you already belong to, and
        # being invited somewhere is invisible.
        mine = teams.invites_for(root, user.get("email"))
        if not t:
            return {"team": None, "members": [], "invites": [],
                    "my_invites": mine, "email_configured": notify.is_configured()}
        rows = []
        for m in teams.members(root, t["team_id"]):
            who = users.get(m["user_id"]) or {}
            try:
                live = len(registry.agents_for_user(root, m["user_id"]))
            except Exception:                              # noqa: BLE001
                live = 0
            rows.append({"user_id": m["user_id"], "email": who.get("email") or "",
                         "role": m.get("role"), "agents_live": live,
                         "is_you": m["user_id"] == uid})
        return {"team": t, "members": rows,
                "invites": teams.pending_invites(root, t["team_id"]),
                "my_invites": [i for i in mine if i["team_id"] != str(t["team_id"])],
                "email_configured": notify.is_configured()}

    def _team_admin(self, user):
        """``(team, None)`` when this user may change the team, ``(None, response)`` when
        not. Membership alone is not enough — only an admin mutates."""
        t = teams.team_for_user(self.sessions_root, user["user_id"])
        if not t:
            return None, None                              # caller decides (invite creates)
        # "owner" comes from the database trigger that creates the first membership; it
        # is the strongest role there is, and requiring exactly "admin" locked the
        # person who created the team out of administering it.
        if t.get("role") not in teams.ADMIN_ROLES:
            return None, {"error": "not_admin"}
        return t, None

    def agent_safety_rules(self, agent):
        """The escalation rules this agent stops for — what frame 10 collected.

        They ride in the interview record as the reserved SAFETY_RULES_ID answer, keyed
        by the agent's bundle inside its build session, so this is a file read rather
        than a query. Empty string when the agent predates frame 10 or the session's
        artifacts are gone; the Rules tab says so rather than showing a blank.
        """
        sid, bid = agent.get("session_id"), agent.get("bundle_id")
        if not sid or not bid:
            return ""
        try:
            rec = sessions.read_json_artifact(
                self.sessions_root, sid, sessions.interview_filename(bid)) or {}
            for a in rec.get("answers") or []:
                if a.get("q") == SAFETY_RULES_ID:
                    return str(a.get("answer") or "").strip()
        except Exception:                                  # noqa: BLE001 — never break the page
            pass
        return ""

    def agent_app_slugs(self, agent):
        """The app slugs this agent actually works in, from its connection record.

        Falls back to the legacy ``staged_apps`` list for agents deployed before
        approval became a whole-agent setting — that column doubled as the only record
        of which apps an agent touched, and lesson provenance still needs it.
        """
        legacy = [s for s in (agent.get("staged_apps") or []) if s]
        try:
            rec, _ = self._load_connection(agent.get("session_id"))
            usable = agent_deploy._usable_app_slugs(rec)
        except Exception:                                  # noqa: BLE001 — best-effort
            usable = []
        return list(dict.fromkeys(list(usable) + legacy))

    def agent_available_apps(self, agent):
        """The agent's usable connected apps, for the profile's app list.

        Each app used to carry its own ``staged`` flag driving a per-app approval
        toggle. Approval is one setting for the whole agent now (see
        ``set_agent_mode``): with two deployments — one that says stage, one that says
        execute — "hold Gmail but let Sheets run" is not a mode a deployment can be in.
        """
        approval = agent_config.approval_required(agent)
        return [{"slug": s, "label": agent_runner._app_label(s), "staged": approval}
                for s in self.agent_app_slugs(agent)]

    def agent_activity(self, agent, limit=10):
        """The agent's recent runs, served ENTIRELY from the registry — no platform
        calls, no model calls, nothing that can be slow on a page load.

        Everything this screen shows is already persisted: the plain-English summary
        and the derived per-session status in session_summaries, staged proposals in
        pending_actions, open questions in agent_decisions. Reading the session events
        (and summarizing them) is what produces those rows, and that work now happens
        in refresh_activity on a background thread — because doing it inline meant the
        response waited on up to ten platform round trips plus model calls, which is
        why this page could not load inside the browser's budget however tightly the
        work was capped.

        A session with nothing cached yet renders from its run row (the UI already
        says "No summary yet") and fills in once the refresh lands.
        """
        root = self.sessions_root
        owner = agent.get("owner_user_id")
        staged = agent_config.approval_required(agent)
        runs = list(registry.runs_for_agent(root, agent.get("agent_id"), limit))
        sids = []
        seen = set()
        for run in runs:
            sid = run.get("run_session_id")
            if sid and sid not in seen:
                seen.add(sid)
                sids.append(sid)
        cached = registry.summaries_many(root, sids) if sids else {}
        # One read each, not one per run: both pull the user's whole history and
        # filter in Python, so per-run reads made a page cost grow with its own length.
        pending_rows, decision_rows = [], []
        try:
            if staged:
                pending_rows = list(registry.pending_for_user(root, owner, status=None))
            decision_rows = list(registry.decisions_for_user(root, owner, status="pending"))
        except Exception:                                  # noqa: BLE001 — a history read
            pending_rows, decision_rows = [], []           # must not cost the page

        out = []
        seen = set()
        for run in runs:
            sid = run.get("run_session_id")
            entry = {"ran_at": run.get("ran_at"), "run_session_id": sid,
                     "status": run.get("status"), "summary": run.get("summary") or "",
                     "proposals": [], "decisions": []}
            if not sid or sid in seen:
                out.append(entry)
                continue
            seen.add(sid)
            row = cached.get(sid) or {}
            # The cached status is ground truth (derived from the real events); the run
            # row's status is its self-report at insert time and, for deployment runs,
            # is recorded at session START and never updated.
            if row.get("status"):
                entry["status"] = row["status"]
            if row.get("summary"):
                entry["summary"] = row["summary"]
            if staged and run.get("kind") != "approval":
                entry["proposals"] = self._proposals_for_session(sid, pending_rows)
            if entry["status"] == "needs_input":
                entry["decisions"] = self._decisions_for_session(sid, decision_rows)
            out.append(entry)
        return out

    def activity_needs_refresh(self, agent, limit=10):
        """True when some session in the window still needs the background refresh:
        either it has no cached status yet, or its cached status is the non-terminal
        "running". Keying only on a MISSING status stranded finished runs on "running"
        — a status derived while the session was still mid-run got cached, and this
        gate then reported "nothing to do", so it was never re-derived after the
        session went idle. Terminal statuses (completed/failed/staged/needs_input)
        stay cached and don't re-fetch. Cheap: two registry reads.

        A session that STAGED and has no pending row is the one terminal status worth
        re-visiting. Its proposals were parsed (that is what makes the status "staged")
        and then not persisted, which is the state every session left behind while the
        write path was gating on the retired column. Without this the fix above only
        helps future runs and the staged work already sitting on the platform stays
        invisible for good. Self-limiting: session_status says "staged" only when
        extract_proposals found something, and _session_proposals inserts from that same
        parse, so one refresh closes the gap and this goes quiet."""
        root = self.sessions_root
        try:
            runs = registry.runs_for_agent(root, agent.get("agent_id"), limit)
            sids = [r.get("run_session_id") for r in runs if r.get("run_session_id")]
            sids = [s for s in sids if s not in self._sessions_gone]
            if not sids:
                return False
            cached = registry.summaries_many(root, sids)
            if any((cached.get(s) or {}).get("status") in (None, "", "running")
                   for s in sids):
                return True
            if not agent_config.approval_required(agent):
                return False
            kinds = {r.get("run_session_id"): r.get("kind") for r in runs}
            # sids is already free of sessions the platform has lost, so one that can
            # never be re-read cannot keep asking to be.
            staged = {s for s in sids
                      if kinds.get(s) != "approval"
                      and (cached.get(s) or {}).get("status") == "staged"}
            if not staged:
                return False
            harvested = {p.get("run_session_id") for p in
                         registry.pending_for_user(root, agent.get("owner_user_id"),
                                                   status=None)}
            return bool(staged - harvested)
        except Exception:                                  # noqa: BLE001
            return False

    def _proposals_for_session(self, run_session_id, rows):
        """This session's staged actions out of an already-read pending list — every
        status, not just pending, so a decided card renders its outcome on reload
        instead of vanishing."""
        out = []
        for p in rows or []:
            if p.get("run_session_id") != run_session_id:
                continue
            # Self-heal a claim orphaned by a crash mid-execution: without this the
            # card would say "Sending…" forever. This screen is where approvals live,
            # so read time here is the reliable place to notice.
            if p.get("status") == "executing" and self._executing_went_stale(p):
                try:
                    registry.pending_set_status(self.sessions_root, p["id"], "failed")
                except Exception:                          # noqa: BLE001
                    pass
                p = {**p, "status": "failed"}
            out.append(p)
        return out

    def _decisions_for_session(self, run_session_id, rows):
        """This session's still-open questions out of an already-read decision list."""
        return [self._decision_for_ui(d) for d in (rows or [])
                if d.get("run_session_id") == run_session_id]

    # --- background enrichment (what used to happen inside the page load) ----
    def maybe_refresh_activity(self, agent, limit=10):
        """Spawn ONE background refresh for this agent if one isn't already running.
        Returns the started Thread (tests join it) or None. Never raises."""
        try:
            aid = agent.get("agent_id")
            if not aid:
                return None
            with self._bg_lock:
                if aid in self._activity_refreshing:
                    return None
                if len(self._activity_refreshing) >= ACTIVITY_MAX_CONCURRENT:
                    return None                            # picked up on a later load
                self._activity_refreshing.add(aid)
            try:
                t = threading.Thread(target=self._activity_refresh_worker,
                                     args=(agent, limit), daemon=True)
                t.start()
                return t
            except Exception:                              # noqa: BLE001 — a failed spawn
                with self._bg_lock:                        # must release the claim
                    self._activity_refreshing.discard(aid)
                raise
        except Exception:                                  # noqa: BLE001
            return None

    def flush_activity_refresh(self, timeout=30):
        """Block until no background activity refresh is in flight. Test/shutdown
        helper, mirroring flush_approvals — the enrichment a page used to do inline
        now happens on a thread, so a caller that wants the enriched rows has to
        wait for it explicitly."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._bg_lock:
                if not self._activity_refreshing:
                    return True
            time.sleep(0.02)
        with self._bg_lock:
            return not self._activity_refreshing

    def _activity_refresh_worker(self, agent, limit):
        try:
            self.refresh_activity(agent, limit)
        except Exception as e:                             # noqa: BLE001
            print(f"[activity] refresh failed for {agent.get('agent_id')}: {_why(e)}",
                  flush=True)
        finally:
            with self._bg_lock:
                self._activity_refreshing.discard(agent.get("agent_id"))

    def refresh_activity(self, agent, limit=10):
        """Read the real session events for this agent's recent runs and persist what
        the activity view reads: the derived status and plain-English summary per
        session, plus any staged proposals and open questions. Returns the number of
        sessions whose cache it wrote. Never raises.

        This is the expensive half of the old activity view. Nothing here is on a
        request path, so it can take the time the platform and the model need.
        """
        root = self.sessions_root
        owner = agent.get("owner_user_id")
        # THE one reader of "does this agent stage?", the same call agent_activity
        # makes. This line read agent["staged_apps"] directly until it was found
        # gating the only path that turns a staged proposal into an approvable row —
        # so every agent deployed after approval became a whole-agent flag (which
        # stopped writing that column) staged its actions into a session nobody
        # harvested, and no approval card ever appeared.
        staged = agent_config.approval_required(agent)
        try:
            client = self._live_client()
        except Exception:                                  # noqa: BLE001 — no key: nothing
            return 0                                       # to refresh from
        try:
            model_fn = self._summarize_model_fn()
        except Exception:                                  # noqa: BLE001
            model_fn = None
        runs = list(registry.runs_for_agent(root, agent.get("agent_id"), limit))
        by_sid, order = {}, []
        for run in runs:
            sid = run.get("run_session_id")
            if sid and sid not in by_sid and sid not in self._sessions_gone:
                by_sid[sid] = run
                order.append(sid)
        if not order:
            return 0

        def fetch(sid):
            """Events for one session, or None. A 404 means the session is gone from
            the platform for good — memoize it so no later pass re-fetches it."""
            try:
                return client.list_session_events(sid)
            except Exception as e:                         # noqa: BLE001
                if getattr(e, "status_code", None) == 404 or "not found" in str(e).lower():
                    self._sessions_gone.add(sid)
                return None

        events_by_sid = dict(zip(order, self._fanout([lambda s=s: fetch(s) for s in order])))
        cached = registry.summaries_many(root, order)
        # Summarize every session that still needs one, concurrently, bounded so a long
        # backlog can't fan out without limit.
        need = [s for s in order
                if events_by_sid.get(s)
                and not (cached.get(s) or {}).get("summary")
                and agent_activity.is_terminal(events_by_sid[s])]
        fresh = {}
        if model_fn is not None and need:
            todo = need[:ACTIVITY_SUMMARY_BUDGET]
            done = self._fanout([
                lambda s=s: agent_activity.summarize_session(
                    events_by_sid[s], agent.get("name"), model_fn) for s in todo])
            fresh = {s: t for s, t in zip(todo, done) if t}

        wrote = 0
        for sid in order:
            events = events_by_sid.get(sid)
            if not events:
                continue
            status = agent_activity.session_status(events)
            # Write the STATUS even when there is no summary — carrying any existing
            # summary through, since summary_set writes both columns. Without this a
            # deployment run stays at its insert-time "ran" forever on the fast path.
            summary = fresh.get(sid) or (cached.get(sid) or {}).get("summary") or ""
            try:
                registry.summary_set(root, sid, summary, status)
                wrote += 1
            except Exception:                              # noqa: BLE001
                pass
            run = by_sid.get(sid) or {}
            try:
                if staged and run.get("kind") != "approval":
                    self._session_proposals(agent, sid, events, owner)
                if status == "needs_input":
                    self._session_decisions(agent, sid, events, owner)
            except Exception:                              # noqa: BLE001 — one bad session
                pass                                       # never stops the rest
        return wrote

    def _session_proposals(self, agent, run_session_id, events, owner, rows=None):
        """Persist this session's proposed actions as pending items (deduped) and return
        them — every status, not just pending, so a decided card renders its outcome on
        reload instead of vanishing. Falls back to the final message text when the agent
        didn't emit a structured block, so nothing is lost."""
        root = self.sessions_root
        agent_id = agent.get("agent_id")
        proposals = agent_activity.extract_proposals(events)
        if not proposals:
            final = agent_runner._final_text(events)
            if final.strip() and agent_activity.is_terminal(events):
                proposals = [{"app": "", "action": "review",
                              "summary": agent_runner._first_line(final) or "Review the proposal",
                              "details": {"description": final}}]
        added = False
        for it in agent_runner._proposals_to_pending(agent_id, proposals, run_session_id):
            # pending_add returns True only on a real INSERT (it dedupes on
            # content_hash), so a re-read of the session's already-staged proposals
            # costs nothing — which is the steady state on every reload.
            added = registry.pending_add(root, {
                "agent_id": agent_id, "owner_user_id": owner,
                "run_session_id": run_session_id, "app": it.get("app"),
                "kind": it.get("kind"), "summary": it.get("summary"),
                "rationale": it.get("rationale"),
                "payload": it.get("payload"), "content_hash": it.get("content_hash")}) or added
        # ``rows`` lets a caller supply an already-read pending list; it must be
        # REFRESHED when this session just inserted proposals, or they read back as
        # none. The read/shape half lives in _proposals_for_session, which the (DB-only)
        # activity view uses directly.
        if rows is None or added:
            rows = list(registry.pending_for_user(root, owner, status=None))
        return self._proposals_for_session(run_session_id, rows)

    @staticmethod
    def _executing_went_stale(p, max_minutes=15):
        """True when an "executing" claim is old enough that its process must have died
        (a real send takes seconds to a couple of minutes). Malformed or missing
        timestamps never strand the list — they just read as not stale."""
        import datetime
        try:
            ts = datetime.datetime.fromisoformat(p.get("processed_at") or "")
            now = datetime.datetime.now(ts.tzinfo)
            return (now - ts) > datetime.timedelta(minutes=max_minutes)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _decision_for_ui(d):
        """The one shape the stopped-run card (16) expects.

        The registry stores context, options and rule together in a single JSON blob.
        home_data flattened it; the activity path returned the raw record. So in Session
        history the card rendered the blob straight into the context line — "[object
        Object]" — and, with no options key, never drew the answer chips at all. Same
        card, two callers, two different shapes.
        """
        ctx = d.get("context") or {}
        if not isinstance(ctx, dict):
            ctx = {"context": str(ctx)}
        return {"id": d.get("id"), "question": d.get("question"),
                "context": ctx.get("context") or "",
                "options": [str(o) for o in (ctx.get("options") or [])],
                "rule": ctx.get("rule") or ""}

    def _persist_decisions(self, agent, run_session_id, questions, owner, rows=None):
        """Gap #3: persist a run's needs_input questions as answerable decision records
        (deduped), returning the still-open ones for this session for the UI."""
        root = self.sessions_root
        tags = {"bundle_id": agent.get("bundle_id") or "",
                "workflow": agent.get("name") or agent.get("bundle_id") or ""}
        added = False
        for q in questions or []:
            if not isinstance(q, dict):
                continue
            qt = str(q.get("question") or "").strip()
            if not qt:
                continue
            added = True
            registry.decision_add(root, {
                "agent_id": agent.get("agent_id"), "owner_user_id": owner,
                "run_session_id": run_session_id, "question": qt,
                # `rule` rides in the same JSON blob as options — frame 16's card leads
                # with the rule that stopped the run ("STOPPED — YOUR RULE: NUMBERS THAT
                # LOOK WRONG"), and until now nothing ever put one there, so every
                # stopped run rendered a bare "STOPPED".
                "context": {"context": str(q.get("context") or ""),
                            "options": [str(o) for o in (q.get("options") or [])],
                            "rule": str(q.get("rule") or "").strip()},
                "tags": tags})
        if rows is None or added:                          # see _session_proposals
            rows = list(registry.decisions_for_user(root, owner, status="pending"))
        return self._decisions_for_session(run_session_id, rows)

    def _session_decisions(self, agent, run_session_id, events, owner, rows=None):
        """Persist + return this session's needs_input questions (activity-view path)."""
        return self._persist_decisions(
            agent, run_session_id, agent_activity.extract_questions(events), owner,
            rows=rows)

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
            # Silent until 2026-08-27, which is why eight runs with a WORKING Voyage key
            # produced no agent embedding and nobody could tell whether the feature was
            # off, broken, or simply had nothing to say.
            print(f"[guidance] no query vector for agent "
                  f"{agent.get('agent_id')} -- lesson retrieval skipped this run",
                  flush=True)
            return []
        scopes = registry.agent_lesson_scopes(root, agent)
        kind_emb = registry.agent_embedding_get(root, aid)
        if kind_emb is None:                               # lazily compute + store if missing
            kvecs = agent_embeddings.embed(self._agent_kind_text(agent), input_type="document")
            if kvecs:
                kind_emb = kvecs[0]
                registry.agent_embedding_upsert(root, aid, kind_emb)
        hits = registry.retrieve_lessons(
            root, svecs[0], agent, agent_kind_embedding=kind_emb,
            consume_scope=scopes["consume_scope"],
            # Migration 018 has been applied and its parameters went unused — team scope
            # kept collapsing to owner-only for want of this argument.
            team_owners=teams.owner_ids(root, agent.get("owner_user_id")))
        hits = self._same_context_only(root, agent, hits)
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

    def _same_context_only(self, root, agent, hits):
        """Drop cross-owner lessons learned somewhere this agent does not work.

        The similarity gate upstream compares two agents' DESCRIPTIONS — prose a model
        wrote from a capture — and calls that "same kind of agent". Identical workflows
        worded differently score low; unrelated ones in similar corporate language score
        high. It is a similarity of writing standing in for a similarity of work.

        A shared app is not a similarity, it is a fact. An agent either touches Shopify
        or it does not, and a rule learned while working in an app is far likelier to
        hold for someone else in that same app. So this ANDs a factual test onto the
        fuzzy one, for other people's lessons only.

        Deliberately permissive when either side is UNRECORDED. Every decision written
        before migration 020 has no source_apps, and an agent whose connection record
        cannot be read has none either — treating unknown as "no overlap" would silently
        switch off cross-user sharing for the whole existing corpus, which is a far
        worse failure than admitting a lesson that turns out not to apply (the human
        still has to confirm it).
        """
        cross = [h for h in hits if h.get("tier") != "own"]
        if not cross:
            return hits
        mine = set(self.agent_app_slugs(agent))
        if not mine:
            return hits                                    # unknown -> do not filter
        by_id = registry.decisions_source_apps(root, [h["decision_id"] for h in cross])
        keep = []
        for h in hits:
            theirs = by_id.get(str(h["decision_id"]))
            # own-tier, or provenance we never recorded -> unchanged behaviour
            keep.append(h if (h.get("tier") == "own" or not theirs
                              or mine & set(theirs)) else None)
        return [h for h in keep if h is not None]

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
        # Provenance, captured HERE because it is only knowable here: which apps the
        # source agent works in, and which org it belongs to. Recorded at distillation
        # time or not at all — an agent can be renamed, reconfigured or deleted long
        # before anyone asks whether its lesson applies to someone else.
        if agent:
            team = teams.team_for_user(root, agent.get("owner_user_id")) or {}
            registry.decision_set_provenance(
                root, did,
                source_apps=self.agent_app_slugs(agent),
                source_team_id=team.get("team_id"))
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
            # Claim, not read: the row flips atomically, so a reload (or a racing
            # second click) during the multi-second execution can't approve the
            # same action again.
            it = registry.pending_claim(root, pid, agent.get("owner_user_id"))
            if it:
                items.append(it)
        return self.execute_claimed_actions(agent, items)

    def execute_claimed_actions(self, agent, items):
        """Perform already-claimed staged actions in ONE fresh session and mark each
        executed. Runs on the agent's approval-queue worker, so the items may have
        waited in status "queued" — the flip to "executing" here (not at claim time)
        is what starts the stale-executing clock. Never raises."""
        root = self.sessions_root
        if not items:
            return {"status": "failed", "summary": "Nothing to approve."}
        for it in items:
            registry.pending_set_status(root, it.get("id"), "executing")
        agent = self._with_owner(agent)
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
            "run_session_id": res.get("run_session_id"), "kind": "approval"})
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
                                if a.get("q") in (EXTRA_NOTES_ID, SAFETY_RULES_ID)
                                and str(a.get("answer", "")).strip()]
                    rec = _interview_record(b["id"], questions, answers)
                    _write_json(sessions.path(root, sid, sessions.interview_filename(b["id"])), rec)
                    return b["id"]

                errs = []
                done = self._fanout([lambda b=b: _questions_for(b) for b in approved],
                                    errors=errs)
                job["generated"] = sum(1 for d in done if d)
                failed = [b["id"] for b, d in zip(approved, done) if d is None]
                if failed:
                    # Surface which workflows failed instead of aborting the whole step —
                    # the ones that succeeded keep their questions — and say WHY. The
                    # reason was being caught in _fanout and dropped, so this line named
                    # a bundle id and nothing a person could act on.
                    why = f" ({type(errs[0]).__name__}: {errs[0]})" if errs else ""
                    raise RuntimeError("couldn't prepare questions for: "
                                       + ", ".join(failed) + why)
                sessions.update_meta(root, sid, interview_at=sessions._now())
                self._sync_interview_complete(sid)   # zero-approved => vacuously complete
                job.update(state="done")
            except Exception as e:
                job.update(state="error", error=_why(e))

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
        rules = str((answers_map or {}).get(SAFETY_RULES_ID, "") or "").strip()
        if rules:                                      # frame 10: same shape, same rules
            answers.append({"q": SAFETY_RULES_ID, "question": SAFETY_RULES_LABEL,
                            "answer": rules})
        rec = _interview_record(bid, questions, answers)
        _write_json(sessions.path(self.sessions_root, sid, sessions.interview_filename(bid)), rec)
        self._sync_interview_complete(sid)
        return rec

    def _interview_all_complete(self, sid):
        """True when EVERY approved bundle's interview record exists with status
        'complete' (vacuously true with zero approved bundles). The questions phase
        always RUNS before build — this is the build gate, always recomputed from disk —
        but it may legitimately produce NO questions, which ``_interview_record`` scores
        as 'complete', so a workflow with no real gaps passes straight through."""
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
                job.update(state="error", error=_why(e))

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
            # An agent has up to two deployments (staging + execution); cleanup that only
            # knew about the active one left the paused sibling behind on the platform.
            deployment_ids.extend(v for v in (d.get("deployments") or {}).values() if v)
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
        # A memory store is reused across redeploys of its workflow (keyed on user +
        # workflow name), so a re-recorded workflow's NEW agent can mount the store
        # this session's deploy created: never delete a store that a registered agent
        # from ANOTHER capture session still uses.
        if memory_store_ids:
            try:
                keep = {a.get("memory_store_id")
                        for a in registry.agents_all(self.sessions_root)
                        if a.get("memory_store_id") and a.get("session_id") != sid}
            except Exception:                              # noqa: BLE001 — deletion is the risky path,
                keep = None                                # so an unknown registry state skips it
            memory_store_ids = ([m for m in memory_store_ids if m not in keep]
                                if keep is not None else [])
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
        # The cloud copy. Uploaded since Storage landed and, until now, unreachable by
        # any delete path — the privacy screen promised "your device and the cloud" and
        # only the first half was true.
        cloud = sessions.purge_cloud_artifacts(root, sid)
        shutil.rmtree(sessions.session_dir(root, sid), ignore_errors=True)
        return {"session_id": sid, "deleted": True, "platform": platform, "cloud": cloud}

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
                job.update(state="error", error=_why(e))

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
                job.update(state="error", error=_why(e))

        threading.Thread(target=run, daemon=True).start()

    # --- Phase 6: live deployment ----------------------------------------
    def _refresh_stale_bindings(self, sid, conn):
        """WS5: re-mint + rotate the Pipedream token for EVERY usable app at deploy, so
        a run long after Connect still authenticates — no manual re-verify. Refreshing
        all usable apps (not just stale ones) is load-bearing for URL migrations:
        vault credentials are keyed to the exact MCP server URL, and refresh_binding
        falls back to a full bind when no credential matches the current URL — so a
        redeploy after a pipedream_mcp_url format change self-migrates the vault.
        Deploys are infrequent; the extra token mints are a few cheap calls.
        Best-effort in that a refresh failure never blocks the deploy attempt — but
        failures are RETURNED (``(conn, errors)``) so the deploy job can surface
        them instead of reporting green: a swallowed rebind failure is how agents
        once shipped "connected" with no run-time bearer. The hard gate is
        deploy_live's binding check."""
        errors = {}
        vault_ref = (conn or {}).get("vault_ref")
        if not vault_ref:
            return conn, errors
        uid = conn.get("external_user_id")
        usable = [slug for slug, a in (conn.get("apps") or {}).items()
                  if a.get("usable")]
        if not usable:
            return conn, errors
        try:
            client = self.connect_fn()
        except Exception as exc:
            return conn, {"_client": str(exc)}
        changed = False
        for slug in usable:
            try:
                out = client.refresh_binding(vault_ref, uid, slug)
                conn["apps"][slug]["token_expires_at"] = out.get("expires_at")
                changed = True
            except Exception as exc:
                errors[slug] = str(exc)
        if changed:
            self._save_connection(sid, conn)
        return conn, errors

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
        job = {"state": "running", "error": None, "deployed": 0, "errors": {},
               "warnings": []}
        self.deploy_jobs[sid] = job

        def run():
            try:
                eligible, _, conn = self._deploy_eligible(sid)
                conn, bind_errors = self._refresh_stale_bindings(sid, conn)  # WS5 + URL migration
                # The deploy names everything it creates (deployment, environment,
                # memory store) for the OWNER — hand it the account email when the
                # session has one. Transient enrichment; the record on disk keeps its
                # shape for sessions without an owner (the founder Test UI).
                owner_uid = sessions.owner_of(root, sid)
                if owner_uid and not (conn or {}).get("owner_email"):
                    try:
                        conn = {**(conn or {}),
                                "owner_email": (users.get(owner_uid) or {}).get("email")}
                    except Exception:                      # noqa: BLE001 — naming must never block a deploy
                        pass
                # The isolation key, from the same owner, for the same reason. It was
                # only ever read out of connection.json — a file on the container disk
                # Render wipes — even though users.external_user_id is a pure function of
                # the user id and cannot fail. When it went missing the memory store was
                # named without an owner, and two accounts could land on one store.
                if owner_uid and not (conn or {}).get("external_user_id"):
                    conn = {**(conn or {}),
                            "external_user_id": users.external_user_id(owner_uid)}
                if bind_errors:
                    # visible on the deploy job — a rebind failure usually means the
                    # per-bundle deploys below will fail their binding check too
                    job["errors"]["_bindings"] = bind_errors
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
                            owner = owner_uid
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
                                        client = self._live_client()
                                        for did in {prior["deployment_id"],
                                                    *(prior.get("deployments") or {}).values()}:
                                            if did and did not in (
                                                    rec.get("deployments") or {}).values():
                                                client.archive_deployment(did)
                                except Exception:          # noqa: BLE001 — best-effort
                                    pass
                                dropped = registry.agent_upsert(root, {
                                    "agent_id": rec["agent_id"], "owner_user_id": owner,
                                    "external_user_id": rec.get("external_user_id"),
                                    "bundle_id": bid, "name": b.get("name") or bid,
                                    "vault_ref": rec.get("vault_ref"),
                                    "trigger": (rec.get("start") or {}).get("value"),
                                    "session_id": sid,
                                    "approval_required": bool(rec.get("approval_required")),
                                    "deployment_id": rec.get("deployment_id"),
                                    "deployments": rec.get("deployments") or {},
                                    "deployment_mode": rec.get("deployment_mode"),
                                    "memory_store_id": rec.get("memory_store_id"),
                                    # the owner identity every later session/
                                    # environment/deployment is named with
                                    "owner_email": rec.get("owner_email"),
                                    # the worker's review-step correction is the truer bio
                                    "description": b.get("user_description")
                                                   or b.get("description") or "",
                                    # deploy.py has computed both of these all along and
                                    # nothing carried them across, so the agents table
                                    # recorded that a deployment existed but never that
                                    # it went live.
                                    "status": rec.get("status"),
                                    "deployed_at": rec.get("deployed_at")})
                                if dropped:
                                    # The agent IS registered — agent_upsert dropped
                                    # what the database would not take rather than
                                    # lose it — but a dropped column means the row
                                    # says less than the deploy knew. Carry it on the
                                    # job so /deploy/status can say so; the registry
                                    # has already named the cause in the log.
                                    job["warnings"].append(
                                        f"{bid}: registered without {', '.join(dropped)}"
                                        " — a schema migration is pending")
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
                errors_line = "; ".join(f"{k}: {msg}"
                                        for k, msg in job["errors"].items())
                if job["deployed"]:
                    sessions.update_meta(root, sid, deploy_at=sessions._now())
                    job.update(state="done")
                elif job["errors"]:
                    # "No agents were created" describes a creation that was ATTEMPTED
                    # and failed. With nothing eligible the only entries here are the
                    # rebind failures from above, and that sentence sends the reader
                    # looking for a platform error that was never raised.
                    job.update(state="error",
                               error=("No agents were created. " if eligible else
                                      "No agents were eligible to deploy. ")
                                     + errors_line)
                else:
                    job.update(state="done")          # nothing eligible -> nothing to do
            except Exception as e:
                job.update(state="error", error=_why(e))

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

    # --- Approval queue: one MA session at a time per agent --------------
    def enqueue_approval(self, agent_id, fn):
        """FIFO-enqueue one approval-triggered execution. Approvals for the same
        agent run strictly one after another in the order they were approved (a
        single drain thread per agent — a bare Lock wouldn't be FIFO-fair);
        different agents drain independently. Workers start lazily and exit when
        their queue empties, so no main()-time setup is needed."""
        key = str(agent_id or "")
        with self.lock:
            q = self._approval_queues.setdefault(key, queue.Queue())
            q.put({"fn": fn})
            w = self._approval_workers.get(key)
            if w is None or not w.is_alive():
                w = threading.Thread(target=self._drain_approvals,
                                     args=(key, q), daemon=True)
                self._approval_workers[key] = w
                w.start()

    def _drain_approvals(self, key, q):
        while True:
            with self.lock:              # puts also happen under self.lock, so the
                if q.empty():            # empty-check + self-removal is race-free
                    self._approval_workers.pop(key, None)
                    self._approval_queues.pop(key, None)
                    return
            task = q.get()
            try:
                task["fn"]()             # bodies never raise; belt-and-braces anyway
            except Exception:            # noqa: BLE001 — a bad task must not kill the drain
                pass
            finally:
                q.task_done()

    def flush_approvals(self, timeout=10):
        """Block until every queued approval has finished (workers self-terminate
        when drained, so joining them = drained). Test/shutdown helper."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                workers = list(self._approval_workers.values())
            if not workers:
                return True
            for w in workers:
                w.join(timeout=max(0.0, deadline - time.time()))
        with self.lock:
            return not self._approval_workers

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
        # Its OWN lock: this holds across two Supabase calls, and on the global lock
        # that stalled every page load for the length of a scheduler pass.
        with self._runs_lock:
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
                # Learning-cycle trigger. Deployment rows are recorded at session
                # START, so the fresh-row check almost always defers on the
                # terminality gate — the throttled periodic re-check is what
                # actually completes a cycle once the session has ended.
                last_check = self._learning_checked.get(agent_id, 0)
                if fresh or now - last_check >= agent_config.LEARNING_CHECK_SECONDS:
                    self._learning_checked[agent_id] = now
                    self.maybe_run_learning_cycle(agent, verbose=fresh)
            except Exception:                              # noqa: BLE001
                pass                                       # one bad agent never kills the pass

    def _deliver_guidance(self, agent, lessons, rejected=None):
        """Put what Trace knows where a DEPLOYED agent can actually see it: retrieved
        lessons, and the actions this agent's human has recently turned down.

        run_agent passes guidance straight into the session it creates. A deployment
        run has no such door -- run_deployment(deployment_id) takes no input -- so
        until now retrieval simply was not called on that path, and since every real
        agent is deployment-backed, cross-user learning could never happen at all.

        The agent's memory store IS the door: it is mounted at /mnt/memory/ and read at
        the start of every session. Writing shared_lessons.md before triggering the run
        is the deployment-path equivalent of the guidance argument.

        Rejections travel the same way, and for the same reason. They used to be
        prepended to the deployment's opening message, which meant every "no" on an
        approval card destroyed and rebuilt a live platform deployment to change one
        paragraph. Same door, no rebuild.

        Best-effort and loud: a failure here must not stop the run, but it must not be
        invisible either -- a silent miss looks exactly like "no lessons to share".
        """
        store_id = agent.get("memory_store_id")
        if not store_id:
            if lessons or rejected:
                print(f"[guidance] agent {agent.get('agent_id')} has {len(lessons or [])} "
                      f"lesson(s) but no memory_store_id -- deployed before shared "
                      f"lessons existed; redeploy to pick them up", flush=True)
            return False
        head = agent_config.MEMORY_SHARED_SEED.split("(none yet)")[0].rstrip()
        blocks = []
        if lessons:
            blocks.append("\n".join(f"- {l}" for l in lessons))
        rejected_text = agent_runner.rejected_block(rejected).strip()
        if rejected_text:
            blocks.append(rejected_text)
        body = f"{head}\n\n" + "\n\n".join(blocks) + "\n" if blocks else \
            agent_config.MEMORY_SHARED_SEED           # clear stale advice
        try:
            wrote = self._live_client().write_memory(
                store_id, agent_config.MEMORY_SHARED_DOC, body)
        except Exception as e:                             # noqa: BLE001
            print(f"[guidance] could not reach the memory store for "
                  f"{agent.get('agent_id')}: {type(e).__name__}: {e}", flush=True)
            return False
        if wrote and blocks:
            print(f"[guidance] delivered {len(lessons or [])} lesson(s) and "
                  f"{len(rejected or [])} rejection(s) to {agent.get('agent_id')}",
                  flush=True)
        return bool(wrote)

    # --- learning cycles: Fable-driven periodic self-improvement ---------
    def maybe_run_learning_cycle(self, agent, verbose=False, force=False):
        """Best-effort trigger: when >= LEARNING_CYCLE_SESSIONS eligible runs have
        accrued since the last learning cycle, spawn one on a daemon thread (a
        Fable call producing a full replacement doc takes far longer than an HTTP
        handler or a scheduler pass can afford). Eligible = has a run_session_id
        and kind != "approval" — approval sessions run under a different directive
        ("act, don't propose"), are tiny, and would skew the workflow analysis,
        the same reasoning that keeps the activity view from mining them (019).
        Statuses are deliberately NOT filtered: deployment rows are recorded at
        session start and stay "ran" forever, so real terminality is checked
        against live events in the worker. Returns the started Thread (tests
        join it) or None. Never raises.

        ``verbose`` prints the skip reason — passed by the call sites when a run
        just happened, so every scheduled run leaves one line of learning progress
        in the log, while the throttled re-checks stay silent. Without this, a
        skipped agent was indistinguishable from an unchecked one and "why is
        nothing learning?" had no answer in the log.

        ``force`` (the manual "learn now") drops the session-COUNT gate only —
        everything else still applies, including the kill switch, the memory-store
        requirement, the one-cycle-at-a-time bound and terminality. It exists so a
        change to the learning path can be exercised on demand: at the default of
        five sessions and a daily cadence, the alternative was a five-day wait per
        attempt."""
        try:
            aid = agent.get("agent_id")
            if not aid:
                return None
            if not agent_config.LEARNING_ENABLED:           # TRACE_LEARNING_ENABLED=0
                return None
            if not agent.get("memory_store_id"):
                # pre-memory agents keep working rather than erroring
                if verbose:
                    print(f"[learning] {aid} has no memory store — deployed before "
                          f"memory stores existed; redeploy it to enable learning",
                          flush=True)
                return None
            root = self.sessions_root
            cycles = registry.learning_cycles_for_agent(root, aid, limit=1)
            since = str(cycles[0].get("created_at") or "") if cycles else ""
            n = agent_config.LEARNING_CYCLE_SESSIONS
            runs = registry.runs_for_agent(root, aid, limit=max(50, 4 * n))
            batch = [r for r in runs
                     if r.get("run_session_id") and r.get("kind") != "approval"
                     and r.get("run_session_id") not in self._sessions_gone
                     and str(r.get("ran_at") or r.get("started_at") or "") > since]
            if len(batch) < n:
                if not force:
                    if verbose:
                        print(f"[learning] {aid}: {len(batch)} eligible session(s) "
                              f"since the last cycle — a cycle runs at {n}", flush=True)
                    return None
                if not batch:
                    # Forcing cannot conjure material: with nothing since the last
                    # cycle there is literally nothing to analyze, and an empty batch
                    # would surface downstream as a confusing "analysis failed".
                    print(f"[learning] {aid}: nothing to learn from — no eligible "
                          f"session since the last cycle", flush=True)
                    return None
                print(f"[learning] {aid}: forced cycle on {len(batch)} session(s), "
                      f"below the usual {n}", flush=True)
            with self._bg_lock:
                if aid in self._learning_inflight:
                    return None
                # ONE cycle at a time fleet-wide. The per-agent guard alone allowed N
                # concurrent cycles for N agents — each a long analysis call plus a
                # fan-out of platform reads — which competed for the same API headroom
                # as the (synchronous) activity view and helped push it past the
                # browser's budget. A deferred agent simply cycles on the next check.
                if len(self._learning_inflight) >= agent_config.LEARNING_MAX_CONCURRENT:
                    if verbose:
                        print(f"[learning] {aid}: another cycle is running — will pick "
                              f"this batch up on the next check", flush=True)
                    return None
                self._learning_inflight.add(aid)
            try:
                batch = batch[:agent_config.LEARNING_CYCLE_MAX_SESSIONS]
                t = threading.Thread(target=self._learning_cycle_worker,
                                     args=(agent, batch, force), daemon=True)
                t.start()
                return t
            except Exception:                              # noqa: BLE001 — a failed spawn must
                with self._bg_lock:                        # release the flag, or this agent
                    self._learning_inflight.discard(aid)   # could never learn again
                raise
        except Exception:                                  # noqa: BLE001 — never block a run
            return None

    def _learning_cycle_worker(self, agent, batch, force=False):
        try:
            self._run_learning_cycle(agent, batch, force=force)
        except Exception as e:                             # noqa: BLE001
            print(f"[learning] cycle failed for {agent.get('agent_id')}: {_why(e)}",
                  flush=True)
        finally:
            with self._bg_lock:
                self._learning_inflight.discard(agent.get("agent_id"))

    def _run_learning_cycle(self, agent, batch, force=False):
        """Execute one learning cycle: fetch the batch's session events, verify
        terminality, read the memory docs, make ONE Fable analysis call, write the
        sanitized replacement doc, and record the cycle. Returns the cycle record,
        or None when deferred (a session still running) or the model call failed —
        no cycle row is recorded then, so the same batch is retried later; a clean
        "nothing to change" IS recorded, so the counter still resets.

        Firing right after new terminal runs were recorded is the race mitigation:
        the next platform-cron session is typically hours away. The residual race
        with a concurrently-starting session is accepted — the same acceptance
        shared_lessons.md was built on.

        ``force`` lowers the batch-size floor to one session (see
        maybe_run_learning_cycle) and changes nothing else."""
        aid = agent.get("agent_id")
        store_id = agent.get("memory_store_id")
        client = self._live_client()
        def fetch(sid):
            """One session's events, or None when unfetchable. A 404 means the
            session was deleted/expired — permanent, so memoize it and never fetch
            it again; a transient error is None for this pass only."""
            try:
                return client.list_session_events(sid)
            except Exception as e:                         # noqa: BLE001
                if getattr(e, "status_code", None) == 404 \
                        or "not found" in str(e).lower():
                    self._sessions_gone.add(sid)
                return None

        events_by_run = self._fanout(
            [lambda s=r["run_session_id"]: fetch(s) for r in batch])
        # A None slot means the fetch failed — usually a session deleted/expired on
        # the platform (404), which will never become fetchable, so it must not gate
        # the cycle. An existing session with no terminal event ([] included)
        # genuinely is still running and does defer.
        pairs = [(r, ev) for r, ev in zip(batch, events_by_run) if ev is not None]
        gone = len(batch) - len(pairs)
        if not pairs:
            print(f"[learning] deferring for {aid} — all {gone} session(s) in the "
                  f"batch are gone from the platform", flush=True)
            return None
        if not agent_activity.is_terminal(pairs[0][1]):     # newest FETCHABLE session
            print(f"[learning] deferring for {aid} — latest session still running",
                  flush=True)
            return None
        kept = [(r, ev) for r, ev in pairs if agent_activity.is_terminal(ev)]
        # force lowers the FLOOR; it deliberately does not reach the terminality checks
        # above. A session still running has nothing to analyze yet, and pulling it into
        # a cycle now would also drop it from every later batch (batches are filtered on
        # the last cycle's timestamp), so forcing past it would lose the session outright.
        floor = 1 if force else agent_config.LEARNING_CYCLE_SESSIONS
        if len(kept) < floor:
            print(f"[learning] deferring for {aid} — only {len(kept)} fetchable "
                  f"terminal session(s) in the batch ({gone} gone)", flush=True)
            return None
        # Oldest first, so the model reads the batch in the order it happened.
        records = [agent_learn.session_record(r, ev) for r, ev in reversed(kept)]
        # What this agent actually DOES, taken from the events this batch already
        # fetched. Both halves of the collective path read it — who this agent may learn
        # from, and what it publishes at the end — and it is computed here because these
        # event lists are the only ones in hand; deriving it later would mean a second
        # fan-out of platform reads for material we already paid for once.
        fp = agent_peers.fingerprint([ev for _, ev in kept])
        vec = self._peer_vector(agent, fp)
        # The WHOLE store, listed rather than named. The engine may create and delete
        # documents, so the set is whatever is actually there — reading three known
        # paths would hide from it the documents it wrote itself last cycle, and it
        # would then reason about a store it had already changed. One call, not one
        # per path. A doc that cannot be read stays None -> "(unavailable)" in the
        # payload; never substitute a seed, which reads as wiped memory. If the listing
        # itself fails, fall back to live state alone — it is the only document a store is
        # guaranteed to have, and sending an empty store would invite a rewrite from
        # nothing.
        try:
            docs = client.list_memory(store_id)
        except Exception as e:                             # noqa: BLE001 — a cycle is worth
            print(f"[learning] listing the memory store for {aid} raised "     # more than
                  f"{_why(e)}", flush=True)                                    # a listing
            docs = None
        if docs is None:
            print(f"[learning] could not list the memory store for {aid} — falling "
                  f"back to live state alone", flush=True)
            # Status only: it is the one document guaranteed to exist. Everything else
            # in a store is there because the engine created it, so there is no list of
            # names to fall back to — an unlistable store simply reasons on live state.
            docs = {agent_config.MEMORY_STATUS_DOC:
                    client.read_memory(store_id, agent_config.MEMORY_STATUS_DOC)}
        # --- step 1: the gate. Which of these sessions is a MEASUREMENT ------------
        # Facts only, and before any model call: a gate that is a judgement drifts toward
        # whatever answer the loop wants, and the loop wants to pass.
        try:
            pend = registry.pending_for_user(self.sessions_root,
                                             agent.get("owner_user_id"), status=None)
        except Exception as e:                             # noqa: BLE001 — no rejections known
            print(f"[learning] could not read pending actions for {aid}: {_why(e)} — "
                  f"treating the batch as unintervened", flush=True)
            pend = []
        split = agent_gate.partition(list(reversed(kept)), pend)
        valid_ids = {str((r or {}).get("run_session_id") or "") for r, _ in split["valid"]}
        measured = [rec for rec in records if str(rec.get("session_id")) in valid_ids]
        excluded = [{"session_id": str((r or {}).get("run_session_id") or ""),
                     "reason": reason,
                     "transcript": agent_activity._compact(ev or [])[
                         :agent_config.LEARNING_TRANSCRIPT_CLIP]}
                    for r, ev, reason in split["excluded"]]
        if excluded:
            print(f"[learning] {aid}: {len(excluded)} session(s) excluded from the "
                  f"measurement ({', '.join(sorted({e['reason'] for e in excluded}))}) — "
                  f"still read as evidence", flush=True)

        # --- steps 2 and 3: score, then compute in code ----------------------------
        by_id = {str((r or {}).get("run_session_id") or ""): ev for r, ev in split["valid"]}
        for rec in measured:
            rec["input_kb"] = agent_score.input_kb(by_id.get(str(rec.get("session_id"))))
        scored = agent_score.measure(measured, self._score_model_fn()) if measured else None
        if scored:
            print(f"[learning] {aid}: mean performance {scored['mean_performance']} "
                  f"tokens/difficulty over {len(scored['sessions'])} measured session(s), "
                  f"valid rate {split['valid_rate']:.2f}", flush=True)
        else:
            print(f"[learning] {aid}: no measurement this cycle "
                  f"({len(measured)} measurable session(s))", flush=True)

        # --- step 4: judge the pending change, propose the next one ----------------
        changes = registry.changes_for_agent(self.sessions_root, aid,
                                             limit=agent_config.LEARNING_LOG_CYCLES)
        log = agent_learn.log_lists(changes)
        pending = registry.change_pending(self.sessions_root, aid)
        base = self._baseline_for(aid, changes, pending)
        agent_info = {"name": agent.get("name"), "description": agent.get("description"),
                      "trigger": agent.get("trigger")}
        # No lock is held across any of this, and none may be: peer selection, the
        # analysis call and the publish below are minutes of network I/O. Holding
        # self.lock across network I/O is exactly what once left a database-only page
        # waiting behind a scheduler pass, and is why the background locks were split.
        peers = self._collect_peers(agent, fp, vec)
        result = agent_learn.judge_and_propose(
            agent_info, scored, excluded, docs, log, pending,
            self._learning_model_fn(), peers=peers,
            baseline=base.get("performance"), valid_rate=split["valid_rate"],
            base_valid_rate=base.get("valid_rate"))
        if result is None:
            # With propose ON the analysis IS the cycle, so deferring costs only a
            # re-measurement of overlapping sessions. In observe-only the analysis is
            # discarded anyway, and deferring would fold this batch into the next one
            # — a mean over eight sessions is not comparable with a mean over five,
            # and comparing batch means is the entire point of a calibration run.
            if agent_config.LEARNING_PROPOSE or not scored:
                print(f"[learning] analysis failed for {aid} — will retry the batch "
                      f"on a later pass", flush=True)
                return None
            print(f"[learning] {aid}: analysis failed, but the measurement stands — "
                  f"recording the cycle rather than folding this batch into the next",
                  flush=True)
            result = {}

        # --- step 5: settle the verdict, then apply at most one change -------------
        observed = (scored or {}).get("mean_performance")
        self._settle_pending(agent, pending, observed, base, split["valid_rate"], result)
        docs_written, docs_deleted = self._apply_change(agent, result.get("change"),
                                                        docs, observed)
        # The per-session measurement rides on the cycle row beside the mean it was
        # averaged into. Without it a batch is one number with no way to see inside:
        # whether the scorer counted the work that arrived at all, and whether D is
        # absorbing volume or P is still tracking the workload. Both are read from
        # the spread of items against performance, and both were being computed and
        # discarded. Everything here is already in hand — no extra call.
        scored_rows = {str(r["session_id"]): r
                       for r in ((scored or {}).get("sessions") or [])}
        excluded_why = {str(e["session_id"]): e["reason"] for e in excluded}
        stats = []
        for rec in records:
            sid = str(rec["session_id"])
            row = {"session_id": rec["session_id"], "status": rec["status"],
                   "total_tokens": rec["tokens"]["total_tokens"],
                   "estimated": rec["tokens"]["estimated"]}
            sr = scored_rows.get(sid)
            if sr:
                row.update(items_in_scope=sr["items_in_scope"],
                           novel_items=sr["novel_items"], input_kb=sr["input_kb"],
                           difficulty=sr["difficulty"], performance=sr["performance"],
                           note=sr["note"])
            elif sid in excluded_why:
                # Why this session is not in the mean. A batch whose valid rate moved
                # is unreadable without it.
                row["excluded"] = excluded_why[sid]
            stats.append(row)
        cycle = {
            "agent_id": aid, "owner_user_id": agent.get("owner_user_id"),
            "session_stats": stats,
            "total_input_tokens": sum(r["tokens"]["input_tokens"] for r in records),
            "total_output_tokens": sum(r["tokens"]["output_tokens"] for r in records),
            "tokens_estimated": any(r["tokens"]["estimated"] for r in records),
            "analysis": result.get("analysis") or "",
            # The measurement rides on the cycle row: it is what the next cycle uses as
            # its baseline, and what a human reads to see whether the loop is working.
            "mean_performance": (scored or {}).get("mean_performance"),
            "valid_rate": split["valid_rate"],
            # Deletions ride in docs_written with a marker rather than in a new column:
            # 021 is applied in production and a migration to record a rarer event than
            # the writes beside it is not worth an operator step. The prefix keeps the
            # two readable apart in the admin view.
            "docs_written": docs_written + [f"-{p}" for p in docs_deleted],
        }
        registry.learning_cycle_add(self.sessions_root, cycle)
        total = sum(s["total_tokens"] for s in stats)
        est = " (est)" if cycle["tokens_estimated"] else ""
        deleted = f", deleted {', '.join(docs_deleted)}" if docs_deleted else ""
        print(f"[learning] cycle for {aid}: {len(stats)} sessions, {total} tokens{est}, "
              f"wrote {', '.join(docs_written) or 'nothing'}{deleted}", flush=True)
        # Publishing is deliberately the LAST thing the cycle does. It is a second long
        # model call behind a gate that fails closed, so it is the step most likely to
        # fail — and by now everything this cycle owed its own agent (the rewritten
        # lessons doc, the recorded cycle row) is already durable. A rejected or failed
        # publish therefore costs the fleet one bundle and this agent nothing.
        # The docs published are the ones the agent is ACTUALLY running on: the freshly
        # written content for anything that reached the memory store, the pre-cycle text
        # for the rest. Publishing a doc whose write failed would advertise practice this
        # agent never adopted.
        applied = (result.get("change") or {}).get("docs") or {}
        fresh = {p: c for p, c in applied.items() if p in docs_written}
        # Hand the fleet-wide slot back BEFORE publishing. LEARNING_MAX_CONCURRENT is 1, so
        # whatever holds that slot serialises every other agent's cycle behind it — and the
        # scrub is the longest call in this whole path (a verbatim rewrite streams as many
        # tokens as it reads), with the privacy gate behind it. Holding the slot across both
        # would roughly double the window in which no other agent can learn, to publish a
        # bundle nobody is waiting on. The worker's finally still discards; discard is
        # idempotent. _publish_inflight keeps this agent from publishing twice at once,
        # which is the only race the early release opens.
        aid_ = agent.get("agent_id")
        with self._bg_lock:
            self._learning_inflight.discard(aid_)
            if aid_ in self._publish_inflight:
                return cycle
            self._publish_inflight.add(aid_)
        try:
            self._publish_peer_bundle(agent, records, {**docs, **fresh}, fp)
        finally:
            with self._bg_lock:
                self._publish_inflight.discard(aid_)
        return cycle

    # --- collective learning: peers in, a scrubbed bundle out --------------
    def _peer_vector(self, agent, fp):
        """This agent's profile embedding for peer matching, or None.

        None is an ordinary answer rather than a failure: peers.classify still matches on
        tool overlap without a vector, so an install with no embeddings key gets the
        coarser tier instead of no peers at all. Never raises.
        """
        if not agent_config.COLLECTIVE_ENABLED:
            return None
        try:
            vecs = agent_embeddings.embed(agent_peers.profile_text(agent, fp),
                                          input_type="document")
        except Exception as e:                             # noqa: BLE001 — advisory input
            print(f"[learning] could not embed the peer profile for "
                  f"{agent.get('agent_id')}: {_why(e)}", flush=True)
            return None
        return vecs[0] if vecs else None

    def _score_model_fn(self):
        """The difficulty scorer. A DIFFERENT call from the analyst on purpose — see
        score.py — and overridable in tests like every other model seam here.

        Built exactly like _learning_model_fn, including the tap, so a scoring call
        shows up in the admin prompt log beside the analysis it feeds. It used to read
        ``self.score_fn``, which is the DISCOVERY scorer and is always set, so the
        fallback to a real model caller was unreachable: every cycle called
        discovery_scorer.score(system, user), which raises on a string, and score_batch
        swallowed it into "no measurement this cycle". The tests injected over the same
        attribute, so they could not see it.
        """
        if self.learn_score_fn:
            return self._tap("scoring", None, agent_config.SCORE_MODEL,
                             lambda: self.learn_score_fn)
        key = self.keystore.get_key()
        if not key:
            raise RuntimeError("no_api_key")
        os.environ["ANTHROPIC_API_KEY"] = key
        return self._tap("scoring", None, agent_config.SCORE_MODEL,
                         lambda: agent_score._default_model_fn())

    @staticmethod
    def _baseline_for(aid, changes, pending):
        """What the pending change is measured against: the numbers recorded when it was
        applied. ``{"performance": float|None, "valid_rate": float|None}``.

        Taken from the change row rather than recomputed, because the baseline has to be
        the figure that stood BEFORE the change — recomputing it from whatever is most
        recent would compare a change against a world it had already altered."""
        if not pending:
            return {"performance": None, "valid_rate": None}
        return {"performance": pending.get("baseline"),
                "valid_rate": pending.get("base_valid_rate")}

    def _settle_pending(self, agent, pending, observed, base, valid_rate, result):
        """Close out the change awaiting judgment: keep it, or restore the snapshot.

        The decision comes from learn.decide, NOT from the model's stated verdict. The
        analyst authored the change it is being asked to grade, so the one thing it must
        not hold is the pen on the grade; its answer is recorded beside this one and a
        disagreement is worth a log line. Never raises — a cycle that cannot settle a
        verdict must still be able to run."""
        if not pending:
            return
        aid = agent.get("agent_id")
        cid = pending.get("change_id")
        if not agent_config.LEARNING_PROPOSE:
            # Observe-only has to mean the store does not change, or a calibration run
            # is measuring a store that moved under it. _apply_change is already gated;
            # this path was not, and a revert verdict WRITES (via _restore_snapshot).
            # The change stays pending and is judged when propose comes back on —
            # settling it now would file a verdict against a store still holding it.
            print(f"[learning] {aid}: observe-only (TRACE_LEARNING_PROPOSE=0) — change "
                  f"{cid} left pending, not judged", flush=True)
            return
        try:
            decision, threshold, reason = agent_learn.decide(
                pending, observed, base.get("performance"), valid_rate,
                base.get("valid_rate"))
            said = ((result or {}).get("verdict") or {}).get("decision")
            if said and said != decision:
                print(f"[learning] {aid}: the analyst said {said!r} for {cid} and the "
                      f"arithmetic says {decision!r} — the arithmetic stands", flush=True)
            if decision == "revert":
                self._restore_snapshot(agent, cid)
            # decide() speaks in verbs ("keep"/"revert"); the log stores outcomes
            # ("kept"/"reverted"), which is the vocabulary agent_changes_outcome_check
            # allows and log_lists reads. Two vocabularies meeting at one boundary, as
            # with AGENT_STATUS_DB in the registry — and for the same reason: sending the
            # other one is a row the database refuses and a standing list that silently
            # stays empty.
            outcome = {"keep": "kept", "revert": "reverted"}[decision]
            registry.change_settle(self.sessions_root, aid, cid, outcome,
                                   observed=observed, threshold=threshold,
                                   valid_rate=valid_rate,
                                   base_valid_rate=base.get("valid_rate"), reason=reason)
            print(f"[learning] {aid}: change {cid} {outcome.upper()} — {reason}",
                  flush=True)
        except Exception as e:                             # noqa: BLE001 — never lose the cycle
            print(f"[learning] could not settle change {cid} for {aid}: {_why(e)}",
                  flush=True)

    def _restore_snapshot(self, agent, change_id):
        """Put the documents back exactly as they were before ``change_id``.

        A file restore, never a model call: a model asked to undo its own change
        paraphrases it back into existence. Sound only because every document the engine
        may write has one author — the agent writes live state, which the engine cannot
        touch, and the engine writes the rest — so nothing has edited these bytes since."""
        aid, store_id = agent.get("agent_id"), agent.get("memory_store_id")
        docs = registry.snapshot_get(self.sessions_root, aid, change_id)
        if docs is None:
            print(f"[learning] no snapshot for {change_id} on {aid} — cannot revert, "
                  f"the change stays in the store", flush=True)
            return
        client = self._live_client()
        for path, content in (docs or {}).items():
            try:
                if content is None:
                    # Absent before the change: the change created it, so undoing the
                    # change removes it.
                    client.delete_memory(store_id, path)
                else:
                    client.write_memory(store_id, path, content)
            except Exception as e:                         # noqa: BLE001
                print(f"[learning] could not restore {path} for {aid}: {_why(e)}",
                      flush=True)
        print(f"[learning] {aid}: reverted {change_id}, restored "
              f"{len(docs)} document(s)", flush=True)

    def _apply_change(self, agent, change, docs_before, observed):
        """Snapshot, then write, then delete. Returns ``(written, deleted)``.

        Snapshot FIRST: a revert that has nothing to restore is not a revert, so the
        change is not applied at all unless its prior state is safely recorded. The
        snapshot stores None for a path that did not exist, so undoing a creation is a
        deletion rather than a write of empty content.

        Deletions run AFTER the writes. A change that splits one document into two names
        both the new files and the old path; deleting first would leave the agent with
        nothing to read if the writes then failed, whereas this order's worst case is a
        stale document the next cycle can remove again."""
        if not change:
            return [], []
        aid, store_id = agent.get("agent_id"), agent.get("memory_store_id")
        if not agent_config.LEARNING_PROPOSE:
            print(f"[learning] {aid}: observe-only (TRACE_LEARNING_PROPOSE=0) — "
                  f"change {change.get('change_id')} not applied", flush=True)
            return [], []
        paths = list((change.get("docs") or {}).keys()) + list(change.get("delete") or [])
        snapshot = {p: (docs_before or {}).get(p) for p in paths}
        try:
            registry.snapshot_add(self.sessions_root, aid, change["change_id"], snapshot)
        except Exception as e:                             # noqa: BLE001 — no snapshot, no change
            print(f"[learning] could not snapshot before {change['change_id']} for "
                  f"{aid}: {_why(e)} — the change is NOT applied, because a change that "
                  f"cannot be reverted must not be made", flush=True)
            return [], []
        client = self._live_client()
        written, deleted = [], []
        for path, content in (change.get("docs") or {}).items():
            try:
                if client.write_memory(store_id, path, content):
                    written.append(path)
            except Exception as e:                         # noqa: BLE001 — write_memory is loud
                print(f"[learning] could not write {path} for {aid}: {_why(e)}",
                      flush=True)
        for path in (change.get("delete") or []):
            try:
                if client.delete_memory(store_id, path):
                    deleted.append(path)
            except Exception as e:                         # noqa: BLE001 — delete_memory is loud
                print(f"[learning] could not delete {path} for {aid}: {_why(e)}",
                      flush=True)
        try:
            registry.change_add(self.sessions_root, {
                "agent_id": aid, "owner_user_id": agent.get("owner_user_id"),
                "change_id": change["change_id"], "summary": change.get("summary"),
                "technique": change.get("technique"),
                "rationale": change.get("rationale"),
                "paths": written + deleted,
                "peer_informed": change.get("peer_informed"),
                "retry_of": change.get("retry_of"),
                "baseline": observed})
        except Exception as e:                             # noqa: BLE001
            print(f"[learning] could not log change {change['change_id']} for {aid}: "
                  f"{_why(e)}", flush=True)
        print(f"[learning] {aid}: applied {change['change_id']} — "
              f"{change.get('summary') or 'no summary'}", flush=True)
        return written, deleted

    def _collect_peers(self, agent, fp, vec):
        """The scrubbed material from similar agents to put in front of this cycle, or []
        when the collective path is off, nobody matched, or the lookup failed.

        Never raises and never blocks the cycle. A peer is an enhancement to a learning
        cycle; the agent's own sessions are the point of it. Losing a cycle to a failed
        peer lookup would trade the lessons this agent is guaranteed for material it
        merely might have benefited from — so every failure here degrades to solo
        learning, out loud.
        """
        if not agent_config.COLLECTIVE_ENABLED:
            return []
        aid = agent.get("agent_id")

        def note(peer):
            # Tier and overlap only. A peer belongs to a DIFFERENT customer, and the log
            # is the one place its identity could leak back out of a payload that was
            # built (peers.peer_payloads) to carry nothing but a positional label.
            try:
                return f"{peer.get('tier')}, overlap {float(peer.get('tool_overlap')):.2f}"
            except (TypeError, ValueError):                # a bundle from an older build
                return str(peer.get("tier"))

        try:
            bundles = registry.peer_bundles_latest(self.sessions_root)
            selected = agent_peers.select_peers(aid, fp, vec, bundles)
            # Selection ran on fingerprints alone; the material comes back only for the
            # few that won (see registry.peer_bundles_latest on why that read is narrow).
            # A bundle whose material has since gone is dropped rather than sent empty:
            # an empty peer teaches nothing and still spends payload budget.
            filled = []
            for peer in selected:
                bundle = peer.get("bundle") or {}
                material = registry.peer_bundle_material(self.sessions_root,
                                                         bundle.get("agent_id"))
                if not material or not material.get("sessions"):
                    continue
                filled.append({**peer, "bundle": {**bundle, **material}})
            selected = filled
            peers = agent_peers.peer_payloads(selected)
            if not peers:
                # The empty case is logged too. Silence here is what made "why is nothing
                # learning?" unanswerable in this codebase once already: a cycle that
                # found nobody read exactly like a cycle that never looked.
                print(f"[learning] no peer matched {aid} ({len(bundles)} bundle(s) "
                      f"considered) — learning from its own sessions only", flush=True)
                return []
            print(f"[learning] {len(peers)} peer{'' if len(peers) == 1 else 's'} included "
                  f"({'; '.join(note(x) for x in peers)})", flush=True)
            if len(peers) < len(selected):
                print(f"[learning] {len(selected) - len(peers)} further peer(s) matched "
                      f"{aid} but did not fit the payload budget", flush=True)
            return peers
        except Exception as e:                             # noqa: BLE001 — solo, never lost
            print(f"[learning] peer lookup failed for {aid}: {_why(e)} — learning from "
                  f"its own sessions only", flush=True)
            return []

    def _publish_peer_bundle(self, agent, records, docs, fp):
        """Offer this cycle's batch to the fleet: scrub it, have a second model verify the
        scrub, and store the result as this agent's peer bundle. True only when a bundle
        was actually written.

        Rejection is a routine outcome — the gate fails closed on anything it cannot
        clear — so every exit says why: a bundle that silently never appears is
        indistinguishable from a collective path that was switched off. Never raises; the
        caller has already written this agent's own lessons by the time it runs.
        """
        if not agent_config.COLLECTIVE_ENABLED:
            return False
        aid = agent.get("agent_id")
        try:
            # _sanitize_memory drops scrub.UNSHAREABLE_DOCS and anything unreadable, so
            # the whole store can go in as it is — including documents the engine created
            # itself, which are exactly what a similar agent should see. The rule lives
            # there, in code, rather than restated at each call site.
            # The improvement log rides with the bundle. A transcript is untested
            # behaviour; a log entry is a result this agent already paid to obtain, and
            # the failures travel better than the successes — a failure usually reflects
            # the mechanism, which is portable, while a success often depends on what was
            # in this particular store.
            log = registry.changes_for_agent(self.sessions_root, aid,
                                             limit=agent_config.SCRUB_LOG_ENTRIES)
            scrubbed = agent_scrub.scrub_and_verify(records, docs,
                                                    self._scrub_model_fn(),
                                                    self._scrub_verify_model_fn(),
                                                    log=log)
        except Exception as e:                             # noqa: BLE001 — costs one bundle
            print(f"[learning] scrub failed for {aid}: {_why(e)} — nothing published",
                  flush=True)
            return False
        if not scrubbed:
            # scrub_and_verify already printed the specific "[scrub] rejected — ..." line;
            # this one ties that rejection back to the agent and the cycle it came from.
            print(f"[learning] no peer bundle for {aid} — the scrub gate rejected this "
                  f"batch", flush=True)
            return False
        vec = self._peer_vector(agent, fp)
        try:
            registry.peer_bundle_add(self.sessions_root, {
                "agent_id": aid,
                "owner_user_id": agent.get("owner_user_id"),
                "sessions": scrubbed.get("sessions") or [],
                "memory": scrubbed.get("memory") or {},
                "improvement_log": scrubbed.get("log") or [],
                "fingerprint": fp,
                "embedding": vec,
                # scrub_and_verify returns a dict ONLY after the structural check, the
                # leak sweep and the independent privacy model have all passed, so this
                # flag is that pass's real answer and not an optimistic default.
                "verified": True,
            })
        except Exception as e:                             # noqa: BLE001
            print(f"[learning] could not store the peer bundle for {aid}: {_why(e)}",
                  flush=True)
            return False
        print(f"[learning] published a peer bundle for {aid}: "
              f"{len(scrubbed.get('sessions') or [])} scrubbed session(s), "
              f"{len((fp or {}).get('tools') or {})} tool(s)"
              + ("" if vec else " (no profile vector — peers will match it on tool "
                                "overlap alone)"), flush=True)
        return True

    def run_deployment_now(self, agent):
        """Manual run of a deployment-backed agent: refresh the vault, trigger a
        platform run, and record it. Never raises (a failure becomes a failed run)."""
        self._refresh_agent_binding(agent)
        self._vault_refreshed[agent.get("vault_ref")] = time.time()
        # Phase B: the deployment path never retrieved lessons, so agent_embeddings,
        # lesson_promotions and lesson_applications could not leave zero no matter how
        # many times an agent ran. Retrieval must happen BEFORE the platform run, since
        # what it produces is delivered through the memory store the run will read.
        try:
            self._deliver_guidance(agent, self._retrieve_guidance(agent),
                                   rejected=self._rejected_for(agent))
        except Exception as e:                              # noqa: BLE001 — never block a run
            print(f"[guidance] retrieval failed for {agent.get('agent_id')}: "
                  f"{type(e).__name__}: {e}", flush=True)
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

    def _with_owner(self, agent):
        """The agent row + a resolved ``owner_email``, so every session, environment,
        and deployment created for it is named for the user. Rows deployed since the
        naming change persist the email; older rows get a best-effort users lookup,
        and when neither works the naming helpers fall back to the stable
        external_user_id — never anonymous."""
        if agent and not agent.get("owner_email"):
            try:
                email = (users.get(agent.get("owner_user_id") or "") or {}).get("email")
            except Exception:                              # noqa: BLE001 — naming must never block a run
                email = None
            if email:
                agent = {**agent, "owner_email": email}
        return agent

    # --- Instagram-style "My agents": DMs + home feed ---------------------
    def _agent_dm(self, agent, text):
        """Record a proactive message FROM the agent to its owner (failed runs, blocking
        questions, staged approvals). Best-effort — a store error never breaks a run path.

        This is also where the "ping me when an agent is blocked" email leaves from (18):
        every needs-you moment already funnels through here, so one hook covers all of
        them and none can be forgotten later. Both halves are independently best-effort —
        a delivery problem must not cost the user the in-app message too.
        """
        try:
            registry.message_add(self.sessions_root, {
                "agent_id": agent.get("agent_id"),
                "owner_user_id": agent.get("owner_user_id"),
                "sender": "agent", "text": text})
        except Exception:                                  # noqa: BLE001
            pass
        try:
            if notify.is_configured():                     # cheap; skips the lookups
                uid = agent.get("owner_user_id")
                who = users.get(uid) or {}
                notify.agent_needs_you(
                    registry.user_notify_prefs(self.sessions_root, uid),
                    who.get("email"), agent.get("name"), text)
        except Exception:                                  # noqa: BLE001 — never a failed run
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
                named = self._with_owner(agent)
                who, agent_name = agent_config.agent_owner(named), named.get("name") or aid
                env = client.create_environment(
                    agent_config.environment_defaults(who, agent_name))
                sid = client.create_session(
                    aid, env, resources=agent_runner._memory_resources(agent),
                    title=agent_config.session_title("direct message", agent_name, who),
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
    def _fanout(thunks, workers=None, errors=None):
        """Run independent, IO-bound calls concurrently and return results IN ORDER.

        Every fan-out here is one network call per agent — model calls for a broadcast,
        platform sessions for @mentions — so they were waiting on the sum of the parts
        for no reason. A thunk that raises yields None rather than losing the whole set;
        callers substitute their own fallback.

        ``errors`` is an optional list the exceptions are appended to. Without it a
        failure here is completely silent, and the caller can only report THAT
        something failed: the interview step said "couldn't prepare questions for: B1"
        for a user with no way to learn why, because the reason was caught and dropped
        one frame below. A fallback should replace a result, never the explanation.
        """
        thunks = list(thunks or [])
        if not thunks:
            return []

        def _record(e):
            if errors is not None:
                errors.append(e)
            # Render's logs are the only other place anyone can look.
            print(f"[fanout] {type(e).__name__}: {e}", flush=True)

        if len(thunks) == 1:                               # no thread for a single call
            try:
                return [thunks[0]()]
            except Exception as e:                         # noqa: BLE001
                _record(e)
                return [None]

        def _safe(fn):
            try:
                return fn()
            except Exception as e:                         # noqa: BLE001
                _record(e)
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
        approval = agent_config.approval_required(agent)
        directive = agent_config.GROUP_DIRECT_DIRECTIVE.replace("{text}", text)
        # The staging contract is in an approval-required agent's system prompt, so a
        # directed message only has to name the mode; an autonomous agent gets none.
        trigger = (f"{agent_config.STAGING_INITIAL_MESSAGE}\n\n{directive}"
                   if approval else directive)
        try:
            client = self._live_client()
            named = self._with_owner(agent)
            who, agent_name = agent_config.agent_owner(named), named.get("name") or aid
            env = client.create_environment(
                agent_config.environment_defaults(who, agent_name))
            sid = client.create_session(
                aid, env, resources=agent_runner._memory_resources(agent),
                title=agent_config.session_title("group message", agent_name, who),
                vault_ids=[agent["vault_ref"]] if agent.get("vault_ref") else None)
            events = client.send_trigger_and_collect(sid, trigger)
        except Exception:                                  # noqa: BLE001
            return {**base, "reply": "I couldn't respond just now — try again in a moment."}
        human = agent_runner._final_text(events).split("```")[0].strip()   # drop any proposal fence
        reply = agent_runner._first_line(human) or "Done."
        staged = self._session_proposals(agent, sid, events, owner) if approval else []
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
                 "payload": p.get("payload") or {}, "status": p.get("status")})
        decisions_by_session = {}                           # Gap #3: unanswered questions
        for d in registry.decisions_for_user(root, uid, status="pending"):
            decisions_by_session.setdefault(d.get("run_session_id"), []).append(
                self._decision_for_ui(d))
        confirms_by_agent = {}                              # Phase B: shared lessons to confirm
        for p in registry.lesson_promotions_for_user(root, uid, status="provisional"):
            confirms_by_agent.setdefault(p.get("agent_id"), []).append(
                {"decision_id": p.get("decision_id"), "lesson": p.get("lesson")})
        active_by_agent = {}                                # Phase B: confirmed lessons (retirable)
        for p in registry.lesson_promotions_for_user(root, uid, status="confirmed"):
            active_by_agent.setdefault(p.get("agent_id"), []).append(
                {"decision_id": p.get("decision_id"), "lesson": p.get("lesson")})
        agents_out = []
        # READ path: teammates' agents are visible here and nowhere else. Every
        # mutation still resolves through agents_for_user, which cannot see them.
        all_agents = registry.agents_visible_to(
            root, uid, teams.owner_ids(root, uid))
        # Batched: one query each instead of three per agent (the home screen used to
        # cost 3N round trips and got slower with every agent deployed).
        agent_ids = [a.get("agent_id") for a in all_agents]
        reviewed_by_agent = registry.reviewed_keys_many(root, agent_ids)
        runs_by_agent = registry.runs_for_agents(root, agent_ids, 20)
        schedules_by_agent = registry.schedules_for_agents(root, agent_ids)
        # The fourth batched read. summary_get was still being called inside the per-run
        # loop below — up to 20 round trips per agent, sequentially, before Home painted,
        # and worse with every agent deployed. Its three neighbours were batched for
        # exactly this reason and this one was missed.
        summaries = registry.summaries_many(root, [
            r.get("run_session_id") for runs in runs_by_agent.values() for r in runs])
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
                note = (summaries.get(sid) or {}).get("summary") if sid else None
                stories.append({"key": key, "ran_at": r.get("ran_at"),
                                "started_at": r.get("started_at"),
                                "ended_at": r.get("ended_at"),
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
                # Without this the home + messages screens fall back to a derived color
                # and the agent's CHOSEN color only appears where the server resolves it
                # (group replies) — the same omission _agent_to_caller had.
                "color": registry.agent_color(a),
                "bundle_id": a.get("bundle_id"),
                # frame 25 lists each agent's sharing override beside the account dials.
                # Straight off the row — no extra query per agent.
                "lesson_contribute_scope": a.get("lesson_contribute_scope"),
                "lesson_consume_scope": a.get("lesson_consume_scope"),
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
                # agents_visible_to marks a teammate's agent read-only, and this dict
                # dropped both fields on the floor -- so every screen saw a shared agent
                # as one of your own and offered Run now, Message, schedule changes and
                # capture deletion on it. The server refused all of them (they resolve
                # through agents_for_user), so nothing cross-user ever happened, but the
                # UI spent clicks on buttons that could only 404. Same failure as color,
                # owner_email, ended_at and status: written upstream, never mapped out.
                "can_edit": a.get("can_edit", True),
                "shared_from": a.get("shared_from"),
                "replying": (self.chat_jobs.get(aid) or {}).get("state") == "running"})
        # Frame 14's strip. Two figures, both counted; the frame's third — hours of
        # busywork handled — has nothing behind it, so it is left out rather than guessed.
        week = registry.week_stats(root, uid, agent_ids,
                                   sessions.iso_days_ago(7))
        return {"agents": agents_out, "unread_total": sum(unread.values()),
                "needs_you_total": sum(a["needs_you"] for a in agents_out),
                "week": week}

    def apply_platform_schedule(self, agent, cadence, approval_required=None, mode=None):
        """Re-point a deployment agent at the requested cadence. Deployments are
        immutable, so this creates a fresh PAIR (staging + execution, or execution alone
        for an autonomous agent) and archives the old ones — cron for daily/weekly
        (config.cadence_to_cron), or manual deployments when ``cadence`` is
        None/unmappable so Run-now keeps working. Raises on platform rejection (the
        caller surfaces the real error rather than phantom-succeeding).

        Ordering matters: CREATE the new deployments first, PERSIST them, and only THEN
        archive the old ones. If the create fails, the old (working) deployments stay
        live and still registered — a failed schedule change never strands the agent
        with an archived deployment. Serialized per-process so two concurrent schedule
        POSTs can't each create a live cron deployment and orphan one.

        The agent's current mode is preserved across the rebuild: whichever deployment
        was unpaused before is the one unpaused after. ``approval_required``/``mode``
        override that — ``set_agent_mode`` passes the mode it is switching INTO, which
        the stored row does not know yet (and must not, until the platform agrees).
        """
        # Its OWN lock: the platform calls below can take minutes, and holding the
        # global lock across them froze capture status, approvals and page loads.
        with self._deploy_lock:
            client = self._live_client()
            aid = agent.get("agent_id")
            # re-read the row so a racing change's deployments are the ones we archive
            current = next((a for a in registry.agents_for_user(
                root=self.sessions_root, owner_user_id=agent.get("owner_user_id"))
                if a.get("agent_id") == aid), agent)
            old = {d for d in [current.get("deployment_id"), agent.get("deployment_id")] if d}
            old |= {d for d in (current.get("deployments") or {}).values() if d}
            approval = (agent_config.approval_required(current)
                        if approval_required is None else bool(approval_required))
            mode = mode or agent_runner.agent_mode({**current,
                                                    "approval_required": approval})
            named = self._with_owner(agent)
            who = agent_config.agent_owner(named)
            workflow = named.get("name") or named.get("bundle_id") or aid
            store_id = agent.get("memory_store_id")
            new_deployments = agent_deploy.create_deployments(   # create BEFORE archive
                client, agent_id=aid,
                environment_id=client.create_environment(
                    agent_config.environment_defaults(who, workflow)),
                owner=who, workflow=workflow,
                approval_required=approval, active_mode=mode,
                schedule=agent_config.cadence_to_cron(cadence) if cadence else None,
                resources=[agent_config.memory_resource(store_id)] if store_id else None,
                vault_ids=[agent.get("vault_ref")] if agent.get("vault_ref") else None,
                metadata={"trace_bundle_id": str(agent.get("bundle_id") or "")})
            new_id = new_deployments[mode]
            registry.set_approval_required(self.sessions_root, aid, approval)
            registry.agent_set_deployment(self.sessions_root, aid, new_id,   # register first
                                          deployments=new_deployments, mode=mode)
            for did in old - set(new_deployments.values()):
                client.archive_deployment(did)               # then retire the old ones
            return new_id

    def set_agent_mode(self, agent, approval_required):
        """Switch an agent between staging and execution, and persist the choice.

        This is the whole point of running two deployments. Approval used to be a set of
        app slugs on the row: the in-app "Run now" path read it live, but a
        platform-fired scheduled run replayed the opening message its deployment was
        created with, so turning approval on or off silently did not apply to the runs
        that matter. Now both deployments already exist, and the switch is one unpause
        plus one pause.

        An agent that has no deployment for the mode it is switching INTO — an autonomous
        agent turning approval on, or one deployed before this change — gets its pair
        built by ``apply_platform_schedule``, keeping the cadence it already has.
        Raises on platform rejection; the caller surfaces the real error.
        """
        aid = agent.get("agent_id")
        required = bool(approval_required)
        mode = agent_config.MODE_STAGING if required else agent_config.MODE_EXECUTION
        if not agent.get("deployment_id"):
            registry.set_approval_required(self.sessions_root, aid, required)
            return None                                    # not deployed: the flag is all there is
        deployments = dict(agent.get("deployments") or {})
        if mode not in deployments:
            # No deployment to switch to (or a pre-migration agent with only one). Rebuild
            # the pair on the current cadence; create_deployments leaves `mode` unpaused.
            sched = registry.schedule_get(self.sessions_root, aid) or {}
            return self.apply_platform_schedule(agent, sched.get("cadence"),
                                                approval_required=required, mode=mode)
        # PLATFORM FIRST, then the row. A row that says "staging" while the execution
        # deployment is the unpaused one is worse than a failed toggle: the agent acts,
        # the screen says it is waiting for you, and nobody finds out until it has.
        with self.lock:
            active = agent_deploy.set_active_mode(self._live_client(), deployments, mode)
        registry.set_approval_required(self.sessions_root, aid, required)
        registry.agent_set_deployment(self.sessions_root, aid, active,
                                      deployments=deployments, mode=mode)
        return active

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
        # Carry the human's stated reason where there is one. "Don't propose this again"
        # tells the agent an action was unwanted; "the customer hasn't confirmed the ship
        # date yet" tells it what to do differently, which is the whole point of asking.
        return [{"summary": r.get("summary"), "app": r.get("app"),
                 "why_rejected": (r.get("payload") or {}).get(registry.REJECT_REASON_KEY, "")}
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
        agent = self._with_owner(agent)      # run sessions/environments named for the user
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
        # Trace-side runs are terminal by this point, so the learning-cycle gate
        # usually passes immediately — the freshest moment to learn from the batch.
        self.maybe_run_learning_cycle(agent, verbose=True)
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
            if agent_config.approval_required(agent) and rec.get("run_session_id"):
                try:
                    events = self._live_client().list_session_events(rec["run_session_id"])
                    staged_props = self._session_proposals(
                        agent, rec["run_session_id"], events, agent.get("owner_user_id"))
                except Exception:                          # noqa: BLE001 — view-time still covers it
                    staged_props = []
            n = (len([p for p in staged_props if p.get("status") == "pending"])
                 or len(rec.get("pending") or [])
                 or (1 if rec["status"] == "needs_approval" else 0))
            if n:
                self._agent_dm(agent, f"I've staged {n} action{'s' if n != 1 else ''} for "
                                      "your approval — open my profile to review them.")
        return rec

    def approve_pending(self, item):
        """Spec 8: perform a staged action on the user's approval (the "quick fresh
        task"), then mark it processed. Runs on the agent's approval-queue worker;
        the item arrives claimed to "queued" and flips to "executing" only now, so
        queue wait never counts against the stale-executing clock. Returns the
        execution record. Never raises."""
        agent = self._agent_row(item.get("agent_id"), item.get("owner_user_id"))
        if not agent:
            registry.pending_set_status(self.sessions_root, item.get("id"), "failed")
            return {"status": "failed", "summary": "The agent is no longer available."}
        registry.pending_set_status(self.sessions_root, item.get("id"), "executing")
        agent = self._with_owner(agent)
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
            "run_session_id": res.get("run_session_id"), "kind": "approval"})
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
        def _app_url(self):
            """Where to send an invited person. Built from the request's own Host so the
            link matches however the sender reached the app — a hard-coded base URL is
            wrong for whichever of local / Render / a custom domain it wasn't written for.
            Honours X-Forwarded-Proto, since Render terminates TLS in front of us."""
            host = (self.headers.get("X-Forwarded-Host")
                    or self.headers.get("Host") or "").strip()
            if not host:
                return "/app"
            proto = (self.headers.get("X-Forwarded-Proto") or "").strip() \
                or ("http" if host.startswith(("localhost", "127.0.0.1")) else "https")
            return f"{proto}://{host}/app"

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
            else — auth, all pipeline actions — is proxied to the cloud."""
            if p == "/health" or p in ("/api/status", "/api/permissions"):
                return True
            # Permission probes describe THIS Mac. Proxied, they would faithfully
            # report the Render container's permissions — which are always absent —
            # and the UI would tell every desktop user to grant something they had
            # already granted.
            if p.startswith("/api/u/permissions") or p.startswith("/api/permissions"):
                return True
            # UI assets are handled by _serve_app_desktop rather than the generic
            # proxy, so they are claimed here in every case. That handler fetches
            # the CLOUD copy first — so a UI deploy reaches an already-installed
            # app, which is what the bundle was always documented to be a fallback
            # for — bounded by a short timeout, dropping to the bundled copy when
            # the cloud is slow, cold or unreachable. Serving the bundle
            # unconditionally, as this did, meant no UI fix could reach a desktop
            # user again without shipping a whole new app build.
            if p == "/app" or p.startswith("/app/"):
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
            # If somebody invited this address, joining is the whole point of the invite.
            # Without this, an invite can be created, listed, resent and cancelled and
            # never once put anybody in a team. Best-effort: a team problem must not cost
            # someone their account.
            try:
                teams.accept_for(root, u.get("email"), u["user_id"])
            except Exception:                              # noqa: BLE001
                traceback.print_exc()
            return self._json_auth(200, {"user": u}, set_token=users.start_session(u["user_id"], token))

        def _auth_login(self, body):
            u, token = users.verify((body or {}).get("email"), (body or {}).get("password"))
            if not u:
                return self._json(401, {"error": "invalid_credentials"})
            # Signup accepts pending invites too, but an invite usually lands on somebody
            # who ALREADY has an account: they log in, never sign up again, and the invite
            # is created, delivered, clicked and still puts nobody in a team.
            try:
                teams.accept_for(root, u.get("email"), u["user_id"])
            except Exception:                              # noqa: BLE001
                traceback.print_exc()
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
            """Spec 4: set/clear/pause an agent's schedule, run it now, or run a
            learning cycle now. Scoped to the logged-in user (only their own agents).
            Run-now and learn-now are async (both take minutes) — the dashboard polls
            /api/u/agents for the outcome."""
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
            if action == "learn-now":
                # A cycle needs LEARNING_CYCLE_SESSIONS real sessions, and at a daily
                # cadence that is five days — so checking any change to the learning
                # path end to end was a week-long round trip per attempt. force=True
                # runs the SAME cycle the scheduler runs, minus the count gate; none of
                # the cycle logic is duplicated here, so what this verifies is the real
                # path and not a test-only imitation of it.
                # Threaded like run-now, and for the same reason: the trigger alone
                # reads runs and cycles from the registry before it defers or spawns,
                # which is not work an HTTP handler should be holding a socket open for.
                threading.Thread(
                    target=lambda: state.maybe_run_learning_cycle(agent, verbose=True,
                                                                  force=True),
                    daemon=True).start()
                return self._json(202, {"state": "learning"})
            return self._json(404, {"error": "not_found"})

        def _team_invite(self, body, user):
            """Invite an address — and CREATE the team if there isn't one. That is the
            whole model: a team exists because somebody was invited to it."""
            email = str((body or {}).get("email", "") or "").strip().lower()
            role = (body or {}).get("role")
            if "@" not in email or "." not in email.split("@")[-1]:
                return self._json(400, {"error": "bad_email"})
            t, denied = state._team_admin(user)
            if denied:
                return self._json(403, denied)
            if not t:
                prof = registry.user_profile_get(root, user["user_id"])
                t = teams.create(root, user["user_id"],
                                 prof.get("org_name") or "Your team")
                if not t:
                    return self._json(500, {"error": "team_create_failed"})
            if email == (user.get("email") or "").strip().lower():
                return self._json(400, {"error": "thats_you"})
            teams.invite(root, t["team_id"], email, role, user["user_id"])
            # The invite row exists either way; delivery is best-effort and the UI is
            # told whether email is even configured, so a silent non-send is visible.
            try:
                notify.team_invite(email, t["name"], user.get("email"),
                                   self._app_url())
            except Exception:                              # noqa: BLE001
                pass
            return self._json(200, state.team_view(user))

        def _team_invite_accept(self, invite_id, user):
            """Take up an invite addressed to you. Deliberately not admin-gated: the
            person accepting is by definition not in the team yet."""
            t, why = teams.accept_invite(root, invite_id, user["user_id"],
                                         user.get("email"))
            if not t:
                return self._json(400 if why == "already_in_a_team" else 404,
                                  {"error": why})
            return self._json(200, state.team_view(user))

        def _team_invite_action(self, invite_id, what, body, user):
            t, denied = state._team_admin(user)
            if denied:
                return self._json(403, denied)
            if not t:
                return self._json(404, {"error": "no_team"})
            pending = {i["id"]: i for i in teams.pending_invites(root, t["team_id"])}
            inv = pending.get(invite_id)
            if not inv:
                return self._json(404, {"error": "not_found"})   # never another team's
            if what == "cancel":
                teams.cancel_invite(root, invite_id)
            else:                                          # resend = refresh + send again
                teams.invite(root, t["team_id"], inv["email"], inv.get("role"),
                             user["user_id"])
                try:
                    notify.team_invite(inv["email"], t["name"], user.get("email"),
                                       self._app_url())
                except Exception:                          # noqa: BLE001
                    pass
            return self._json(200, state.team_view(user))

        def _team_member_action(self, member_id, what, body, user):
            t, denied = state._team_admin(user)
            if denied:
                return self._json(403, denied)
            if not t:
                return self._json(404, {"error": "no_team"})
            mem = teams.members(root, t["team_id"])
            if not any(m["user_id"] == member_id for m in mem):
                return self._json(404, {"error": "not_found"})
            admins = [m for m in mem if m.get("role") == "admin"]
            if what == "role":
                role = (body or {}).get("role")
                # Never leave a team with nobody who can administer it.
                if role == "member" and len(admins) == 1 and admins[0]["user_id"] == member_id:
                    return self._json(409, {"error": "last_admin"})
                teams.set_role(root, t["team_id"], member_id, role)
            else:
                if len(admins) == 1 and admins[0]["user_id"] == member_id:
                    return self._json(409, {"error": "last_admin"})
                teams.remove_member(root, t["team_id"], member_id)
            return self._json(200, state.team_view(user))

        def _pending_action(self, pending_id, action, user, body=None):
            """Spec 8: approve (Trace performs the staged action) or reject (discard) one
            pending item. Scoped to the owner. Approve runs async (a real connector call
            can take time); the dashboard polls /api/u/pending for the outcome."""
            item = registry.pending_get(root, pending_id)
            if not item or item.get("owner_user_id") != user["user_id"]:
                return self._json(404, {"error": "not_found"})
            if action == "reject":
                if item.get("status") != "pending":
                    return self._json(409, {"error": "already_processed"})
                registry.pending_set_status(root, pending_id, "rejected",
                                            reason=(body or {}).get("reason"))
                # Nothing more to do here. A rejection used to have to be baked into a
                # freshly-created deployment, because a deployment-backed agent replays
                # the text it was created with and that text is immutable — so every
                # rejected card tore down and rebuilt a live deployment. Rejections now
                # ride in the agent's memory store, which ``_deliver_guidance`` rewrites
                # immediately before each run.
                return self._json(200, {"id": pending_id, "status": "rejected"})
            # Claim before dispatch: the atomic pending→queued flip is what makes a
            # double click (or a click from a stale reload) a 409, not a resend. The
            # send itself runs on the agent's approval queue, one at a time in
            # approval order — concurrent MA sessions from rapid approvals caused
            # real interference.
            item = registry.pending_claim(root, pending_id, user["user_id"],
                                          to_status="queued")
            if not item:
                return self._json(409, {"error": "already_processed"})
            state.enqueue_approval(item.get("agent_id"),
                                   lambda it=item: state.approve_pending(it))
            return self._json(202, {"id": pending_id, "state": "running"})

        def _set_mode(self, agent_id, body, user):
            """Turn approval on or off for one agent, and switch which deployment runs.

            Approval was a per-app set (``{staged_apps: [...]}`` / ``{unstage: slug}``).
            It is one setting for the whole agent now: with a staging deployment and an
            execution deployment, "hold Gmail but let Sheets run automatically" is not a
            state either deployment can be in. The per-proposal "stop staging this app"
            quick action turns the agent autonomous outright, which is a bigger step —
            the UI confirms it.

            Owner-scoped. Takes effect on the NEXT run, and unlike the old toggle it
            takes effect on scheduled platform runs too, not just in-app ones.
            """
            agent = next((a for a in registry.agents_for_user(root, user["user_id"])
                          if a.get("agent_id") == agent_id), None)
            if not agent:
                return self._json(404, {"error": "not_found"})
            b = body or {}
            if "approval_required" not in b:
                return self._json(400, {"error": "approval_required_missing"})
            required = bool(b.get("approval_required"))
            try:
                state.set_agent_mode(agent, required)
            except Exception as e:                          # noqa: BLE001
                traceback.print_exc()
                return self._json(502, {"error": "platform_error", "detail": _why(e)})
            return self._json(200, {"agent_id": agent_id, "approval_required": required,
                                    "mode": (agent_config.MODE_STAGING if required
                                             else agent_config.MODE_EXECUTION)})

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
            if rest == "/capture/status":
                # The controller has always known whether it is running, paused, and for
                # how long — but only POST pause/resume ever returned it. With no way to
                # ASK, leaving the record step mid-capture and coming back showed the idle
                # screen, and Start recording then answered already_running with no route
                # to Stop. Read-only and cheap; safe to call on every mount.
                with state.lock:
                    c = state.controller
                    if not (c and c.running):
                        return self._json(200, {"running": False})
                    s = c.status()
                    s["session_id"] = state.current_sid
                return self._json(200, s)
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
            if rest == "/first-run":                         # §3: new vs returning account
                flags = registry.user_flags_get(root, user["user_id"])
                # An account that already has agents is NOT a first run, whatever the
                # columns say. Without this, an unapplied migration 015 means the flags
                # never persist and every sign-in replays Welcome and then resumes the
                # wizard at the last session's step — landing you on a review of your
                # most recent agent instead of Home.
                if not flags.get("first_run_completed_at"):
                    try:
                        if registry.agents_for_user(root, user["user_id"]):
                            flags = dict(flags, first_run_completed_at="derived",
                                         terms_accepted_at=flags.get("terms_accepted_at")
                                         or "derived")
                    except Exception:                    # noqa: BLE001 — never block sign-in
                        pass
                return self._json(200, flags)
            if rest == "/team":                              # frame 30
                return self._json(200, state.team_view(user))
            if rest == "/profile":                           # frame 18's Account pane
                return self._json(200, dict(
                    registry.user_profile_get(root, user["user_id"]),
                    email=user.get("email") or ""))
            if rest == "/notifications":                     # frame 18's three toggles
                prefs = registry.user_notify_prefs(root, user["user_id"])
                # `email_configured` is the difference between "you turned this off" and
                # "nothing can be sent at all" — the Settings pane should be able to say
                # which, rather than showing switches that quietly do nothing.
                return self._json(200, dict(prefs, email_configured=notify.is_configured()))
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
                # Reading a teammate's runs IS team visibility — without it a shared
                # agent renders as an empty shell claiming zero runs, which is worse
                # than not showing it. Resolved through agents_visible_to, which is
                # read-only by construction; every MUTATION on this agent still goes
                # through agents_for_user and cannot see it.
                agent = next((a for a in registry.agents_visible_to(
                                  root, user["user_id"],
                                  teams.owner_ids(root, user["user_id"]))
                              if a.get("agent_id") == maa.group(1)), None)
                if not agent:
                    print(f"[activity] request agent={maa.group(1)} — NOT VISIBLE to "
                          f"{user.get('user_id')}", flush=True)
                    return self._json(404, {"error": "not_found"})
                # Handler.log_message is a no-op, so no route logs anything by
                # default — which left three rounds of debugging with nothing to read
                # and no way to tell a request that failed from one that never
                # arrived. One line, on the route that has cost the most.
                print(f"[activity] request agent={agent.get('agent_id')} "
                      f"user={user.get('user_id')}", flush=True)
                # An exception escaping here reaches socketserver, which closes the
                # socket having written NOTHING — indistinguishable from a hang to the
                # browser, and the reason Session history sat on "Loading…" forever.
                # A JSON error is something the page can render.
                try:
                    # Kick the background refresh BEFORE building the response, so the
                    # data it writes is ready as soon as possible; the response itself
                    # never waits on it.
                    refreshing = False
                    if state.activity_needs_refresh(agent):
                        refreshing = bool(state.maybe_refresh_activity(agent))
                    return self._json(200, {"agent": {"agent_id": agent.get("agent_id"),
                                                      "name": agent.get("name") or agent.get("bundle_id"),
                                                      "approval_required": agent_config.approval_required(agent),
                                                      "available_apps": state.agent_available_apps(agent),
                                                      # frame 21's Rules tab: the escalation
                                                      # rules set at frame 10, read back
                                                      "rules": state.agent_safety_rules(agent),
                                                      "consent": {
                                                          "effective": registry.agent_lesson_scopes(root, agent),
                                                          "override": {
                                                              "contribute_scope": agent.get("lesson_contribute_scope"),
                                                              "consume_scope": agent.get("lesson_consume_scope")}}},
                                            "activity": state.agent_activity(agent),
                                            # Enrichment (events -> status/summary/
                                            # proposals) runs in the background; the
                                            # page polls while this is true.
                                            "refreshing": bool(refreshing)})
                except Exception as e:                       # noqa: BLE001
                    print(f"[activity] failed for {maa.group(1)}: {_why(e)}", flush=True)
                    traceback.print_exc()
                    return self._json(500, {"error": "activity_failed"})
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
            ma = re.match(
                r"^/agents/([^/]+)/(schedule|schedule/pause|run-now|learn-now)$", rest)
            if ma:                                       # Spec 4: schedule / run-now / learn-now
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
                return self._pending_action(mp.group(1), mp.group(2), user, body)
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
                # Claim to "queued" here (the double-click guard), then hand the batch
                # to the agent's approval queue so back-to-back approvals run their MA
                # sessions one after another in approval order, never concurrently.
                items = [it for pid in ids
                         if (it := registry.pending_claim(root, pid, user["user_id"],
                                                          to_status="queued"))]
                if items:
                    state.enqueue_approval(
                        agent["agent_id"],
                        lambda a=agent, its=items: state.execute_claimed_actions(a, its))
                return self._json(202, {"state": "running"})
            mst = re.match(r"^/agents/([^/]+)/(?:mode|staging)$", rest)
            if mst:                                      # Spec 8: edit which apps are staged (no redeploy)
                return self._set_mode(mst.group(1), body, user)
            man = re.match(r"^/decisions/([^/]+)/answer$", rest)
            if man:                                      # Gap #3: answer a paused question, auto-resume
                return self._answer_decision(man.group(1), body, user)
            if rest == "/lessons/confirm":               # Phase B: confirm/dismiss a shared lesson
                return self._confirm_lesson(body, user)
            if rest == "/first-run/terms":                # §3: terms acceptance, timestamped
                registry.user_flags_set(root, user["user_id"],
                                        terms_accepted_at=sessions._now())
                return self._json(200, registry.user_flags_get(root, user["user_id"]))
            if rest == "/first-run/complete":             # §3: onboarding seen; land on Home next
                registry.user_flags_set(root, user["user_id"],
                                        first_run_completed_at=sessions._now())
                return self._json(200, registry.user_flags_get(root, user["user_id"]))
            # --- team (30). Every mutation is admin-only EXCEPT accept: the person
            # taking up an invite is not in the team yet, so admin is the wrong gate.
            if rest == "/team/invite":
                return self._team_invite(body, user)
            mta = re.match(r"^/team/invites/([^/]+)/accept$", rest)
            if mta:
                return self._team_invite_accept(mta.group(1), user)
            mtr = re.match(r"^/team/invites/([^/]+)/(resend|cancel)$", rest)
            if mtr:
                return self._team_invite_action(mtr.group(1), mtr.group(2), body, user)
            mtm = re.match(r"^/team/members/([^/]+)/(role|remove)$", rest)
            if mtm:
                return self._team_member_action(mtm.group(1), mtm.group(2), body, user)
            if rest == "/profile":                       # frame 18: name + organisation
                b = body or {}
                return self._json(200, registry.user_profile_set(
                    root, user["user_id"],
                    display_name=b.get("display_name"), org_name=b.get("org_name")))
            if rest == "/notifications":                 # frame 18: set the three toggles
                prefs = registry.user_notify_set(root, user["user_id"], **{
                    k: v for k, v in (body or {}).items() if isinstance(v, bool)})
                return self._json(200, dict(prefs, email_configured=notify.is_configured()))
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
            mtpl = re.match(r"^/agents/([^/]+)/use-as-template$", rest)
            if mtpl:
                return self._use_as_template(mtpl.group(1), user)
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
                # `commit` answers the question that cost a whole day: is the code
                # I just pushed actually the code that is running? Render injects
                # RENDER_GIT_COMMIT on every deploy, so this needs no build step and
                # is empty (not wrong) anywhere else.
                return self._json(200, {"status": "ok", "service": "trace-backend",
                                        "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
                                        "database": "supabase" if supabase_client.is_configured()
                                        else "local"})
            if p == "/" or p == "/index.html":
                if not state.test_ui_enabled:
                    # Public deployment: the bare domain lands on the product,
                    # never on the founder Test UI.
                    self.send_response(302)
                    self.send_header("Location", "/app")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self._serve_static("index.html")
            if p.startswith("/static/"):
                if not state.test_ui_enabled:
                    return self._json(404, {"error": "not_found"})
                return self._serve_static(p[len("/static/"):])
            if p == "/app" or p == "/app/":                 # end-user UI (WS2)
                print("[ui] serving /app/index.html", flush=True)
                return self._serve_app_desktop("index.html")
            if p.startswith("/app/"):
                # Logged because "where does the desktop app get its UI?" has been
                # guesswork for two days. If these lines appear on the CLOUD when the
                # desktop app starts, it fetches its UI from here and a deploy reaches
                # it; if they never appear, it is serving its own bundled copy.
                print(f"[ui] serving /app/{p[len('/app/'):]}", flush=True)
                return self._serve_app_desktop(p[len("/app/"):])
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
            # Everything below is the founder Test UI's API: unauthenticated and
            # owner-blind by design, safe only on a loopback bind. On a public
            # bind it does not exist — 404, not 403, so nothing is advertised.
            if not state.test_ui_enabled:
                return self._json(404, {"error": "not_found"})
            if p == "/api/status":
                return self._status()
            if p == "/api/settings":
                return self._json(200, state.keystore.info())
            if p == "/api/settings/pipedream":
                pinfo = getattr(state.keystore, "pipedream_info", None)
                return self._json(200, pinfo() if callable(pinfo) else {"configured": False})
            if p in ("/api/permissions", "/api/u/permissions"):
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
            # Test-UI API from here down (unauthenticated pipeline control, and
            # /api/settings/* which can REPLACE the server's keys): loopback only.
            if not state.test_ui_enabled:
                return self._json(404, {"error": "not_found"})
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
            if p in ("/api/permissions/request", "/api/u/permissions/request"):
                # Triggers the macOS Accessibility dialog. AXIsProcessTrusted on its
                # own returns False forever without ever asking, which is why this
                # permission has sat unrequested since it was first checked.
                permissions.request_accessibility()
                return self._json(200, permissions.check_all())
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
            # Both DELETE routes are Test-UI key management: loopback only.
            if not state.test_ui_enabled:
                return self._json(404, {"error": "not_found"})
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

        def _serve_app_desktop(self, rel):
            """Serve one UI asset. On the desktop node, prefer the CLOUD copy so a
            deployed UI fix reaches an already-installed app — the bundled copy is
            the fallback it was always documented to be (see desktop/trace-node.spec).

            The reason it became local-only was real: proxying every asset meant a
            60s-timeout round trip each, cold starts included, which hung the splash.
            So the cloud attempt is bounded by DESKTOP_UI_TIMEOUT and a failure falls
            straight through to the bundle — a slow or unreachable cloud costs a few
            seconds once, not a broken app. Successful fetches are cached in memory
            briefly so one page load doesn't pay the round trip per file.
            """
            if not state.cloud_backend or os.environ.get("TRACE_DESKTOP_LOCAL_UI") == "1":
                return self._serve_app(rel)
            rel = (rel or "").lstrip("/") or "index.html"
            hit = state.ui_cache_get(rel)
            if hit is not None:
                ctype, data = hit
                return self._text(200, data, ctype, no_cache=True)
            try:
                status, headers, data = state.proxy_fn(
                    state.cloud_backend, "GET", "/app/" + rel, {}, None,
                    timeout=DESKTOP_UI_TIMEOUT)
            except TypeError:                              # proxy_fn without a timeout arg
                try:
                    status, headers, data = state.proxy_fn(
                        state.cloud_backend, "GET", "/app/" + rel, {}, None)
                except Exception:                          # noqa: BLE001
                    return self._serve_app(rel)
            except Exception:                              # noqa: BLE001 — cold/unreachable
                return self._serve_app(rel)
            if status != 200 or not data:
                return self._serve_app(rel)
            ctype = next((v for k, v in headers if k.lower() == "content-type"),
                         _CONTENT_TYPES.get(os.path.splitext(rel)[1],
                                            "application/octet-stream"))
            state.ui_cache_put(rel, ctype, data)
            return self._text(200, data, ctype, no_cache=True)

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
                        "approval_required": agent_config.approval_required(a),
                        "deployments": a.get("deployments") or {},
                        "deployment_mode": a.get("deployment_mode"),
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
                             "questions (as needed)", "build", "connect", "deploy",
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
        def _use_as_template(self, agent_id, user):
            """Start MY OWN build of a workflow a teammate already automated.

            Not a copy of their agent. An agent is welded to its creator's Pipedream
            vault (``trace-user-<owner_id>``), so handing one over would leave it acting
            as the wrong person; and cloning their session would copy a recording of
            their screen into someone else's account, which is not a thing to do
            quietly.

            What travels is the workflow DESCRIPTION — prose already written to be
            shown to the worker, carrying no capture. It seeds a session owned by the
            caller, and the existing describe path writes the same artifacts a capture
            would, so review -> questions -> build -> connect -> go live all run
            unchanged, against this person's own accounts.

            Pull, not push: you can only template an agent you can already see, so
            visibility is the whole permission model and there is nothing new to
            consent to.
            """
            agent = next((a for a in registry.agents_visible_to(
                              root, user["user_id"],
                              teams.owner_ids(root, user["user_id"]))
                          if a.get("agent_id") == agent_id), None)
            if not agent:
                return self._json(404, {"error": "not_found"})
            if not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})

            parts = [str(agent.get("description") or "").strip()]
            trigger = str(agent.get("trigger") or "").strip()
            if trigger:
                parts.append(f"This runs when: {trigger}.")
            rules = str(state.agent_safety_rules(agent) or "").strip()
            if rules:
                parts.append(rules)
            seed = "\n\n".join([p for p in parts if p])
            if not seed:
                return self._json(400, {"error": "nothing_to_template"})

            sid = sessions.create_session(root, label="template",
                                          owner_id=user["user_id"])
            sessions.update_meta(root, sid, owner_user_id=user["user_id"],
                                 described=True, description=seed,
                                 template_of=agent_id)
            with state.lock:
                state.start_describe(sid, seed)
            return self._json(202, {"state": "running", "session_id": sid})

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
            # The questions phase must have RUN for every approved bundle before build
            # (recomputed from disk, never the meta flag). A round that produced no
            # questions is complete — the gate is "asked and answered", not "asked N".
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
            # Spec 8, revised: approval is a whole-agent setting. ``staged_apps`` is
            # still accepted from older clients — a non-empty list means "approve first".
            approval = b.get("approval_required")
            if approval is None:
                approval = bool([x for x in (b.get("staged_apps") or []) if x])
            start = {"trigger": b.get("trigger"), "schedule": b.get("schedule"),
                     "approval_required": bool(approval)}
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
    # The founder Test UI exists only on a loopback bind. Render sets HOST=0.0.0.0,
    # so the cloud deployment locks it automatically; the desktop node and local dev
    # both bind 127.0.0.1 and keep it. Keyed off the CONFIGURED bind, not the request
    # source, so the decision is deterministic and cannot be spoofed by a client.
    # TRACE_ENABLE_TEST_UI=1 is a deliberate operator override — it re-opens
    # unauthenticated access to every session on the host, so never set it on a
    # public service.
    state.test_ui_enabled = (host in ("127.0.0.1", "localhost", "::1")
                             or os.environ.get("TRACE_ENABLE_TEST_UI") == "1")
    httpd = _Server((host, port), make_handler(state))
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
    # Approval-queue boot recovery: a "queued" row's in-memory queue died with the
    # previous process and its action was never sent — release it back to "pending"
    # so the approve button honestly reappears. Skipped on the desktop node (it
    # proxies approvals to the cloud, where the authoritative queue lives), and
    # never run from the enqueue path (it would un-claim freshly queued rows).
    if not state.cloud_backend:
        try:
            registry.pending_release_queued(state.sessions_root)
        except Exception:                # noqa: BLE001 — recovery is best-effort
            pass
    # The OS assigns the real port when PORT=0 (the desktop shell passes 0 so the
    # bind can never race another process) — report the bound port, not the request.
    port = httpd.server_address[1]
    url = f"http://{args.host}:{port}/"
    # Desktop handoff: the shell can't parse stdout (it's redirected to a log file),
    # so when it asks for a portfile, publish the bound port there. Written atomically
    # (tmp + replace) so the shell never reads a torn file.
    portfile = os.environ.get("TRACE_PORTFILE")
    if portfile:
        tmp = portfile + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(port))
        os.replace(tmp, portfile)
    print(f"Trace test UI: {url}", flush=True)
    print(f"  test UI   -> {'enabled (loopback)' if state.test_ui_enabled else 'LOCKED (non-loopback bind; product surface only)'}",
          flush=True)
    print(f"  sessions  -> {os.path.abspath(args.sessions_root)}", flush=True)
    print(f"  collector -> 127.0.0.1:{args.collector_port} (browser extension target)", flush=True)
    if args.cloud_backend:
        print(f"  desktop mode -> proxying auth + pipeline to {args.cloud_backend}", flush=True)
    print("Ctrl-C to stop.", flush=True)
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
