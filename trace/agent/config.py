"""Stage 4 configuration — the editable knobs for the Agent Compiler + Test Deployer.

Everything platform- or policy-specific lives here so it is changed in one place.
Nothing here references a fixture, sender, tab, SKU, or bundle id; the app→MCP table
keys on application identity, not on any workflow's content.
"""

import os
import urllib.parse

# --- platform pins (also mirrored in api_client; keep in sync) -------------
# All doc-confirmed against the Verified Platform Reference (June 2026 public beta).
BETA_VERSION = "managed-agents-2026-04-01"
FILES_BETA = "files-api-2025-04-14"
SKILLS_BETA = "skills-2025-10-02"
AGENT_TOOLSET_TYPE = "agent_toolset_20260401"
MCP_TOOLSET_TYPE = "mcp_toolset"

# --- pipeline defaults (overridable) ---------------------------------------
DEFAULT_MODEL = "claude-sonnet-4-6"
# Manual-evaluation mode (current): evaluation is done by the user in the Console,
# so we keep each run cheap — one session per bundle, a single session's tokens —
# and never fan out. Recent rounds burned credits running many sessions.
DEFAULT_MAX_SESSIONS = 1                 # cost guard: sessions per run
# Bound a sandbox run so it can never hang waiting on the platform: stop a session
# after this wall-clock or event count and clean up anyway (a real run is usually
# under a minute once the environment is warm).
DEFAULT_SESSION_TIMEOUT = 240            # seconds per session
DEFAULT_MAX_EVENTS = 500                 # events per session
REQUEST_TIMEOUT = 120                    # per-HTTP-request read timeout (seconds)
# Transient platform errors (503 overloaded / file storage temporarily unavailable,
# 429, 5xx) are retried with exponential backoff before giving up.
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY = 1.5                   # seconds; doubles each attempt
MOUNT_PATH = "/workspace/files"          # where the Test Deployer mounts artifacts
# Mounted files are READ-ONLY copies; the agent must write outputs to a new path.
# The framing makes this contractual and the Deployer retrieves session-scoped files.
OUTPUTS_PATH = "/workspace/outputs"

# P5 — approval-required policy for outbound-write toolsets. The platform gates a
# call by going idle with a pending tool_use, resolved by replying with a
# `tool_confirmation` event (see api_client.approve_action/reject_action). The enum
# value the Managed Agents platform accepts is `always_ask` (NOT `require_approval`,
# which the API rejects — that broke Spec 8 staged deploys). Never emit always_allow
# by default.
APPROVAL_PERMISSION_POLICY = "always_ask"

# --- Config Engine (Phase 1) -----------------------------------------------
# The Config Engine writes the agent configuration directly (no markdown spec, no
# per-app MCP table). Every live agent reaches the worker's apps through Pipedream;
# the per-customer, per-app MCP server URL is built by ``pipedream_mcp_url`` below.
# Outbound writes/sends are approval-required.
#
# The MCP server is v3 (remote.mcp.pipedream.net); the Connect REST API is v1
# (api.pipedream.com) — two DIFFERENT services, kept as separate constants on purpose.
PIPEDREAM_MCP_BASE = "https://remote.mcp.pipedream.net/v3"
PIPEDREAM_MCP_NAME = "pipedream"        # the mcp_server_name the live toolset references
# Nested object (NOT a bare string). Default: every live toolset (the agent toolset and
# the per-app Pipedream MCP toolsets) runs WITHOUT an approval gate.
LIVE_PERMISSION_POLICY = "always_allow"   # -> permission_policy {"type": "always_allow"}

# --- Phase 4: Connection Layer (Pipedream Connect + Managed Agents vault) ---
# Trace's single Pipedream project + its client-credentials app, sourced from the
# environment so no secret is committed. The MA vault holds only Trace's own Pipedream
# token (never a raw Google token).
PIPEDREAM_PROJECT = os.environ.get("PIPEDREAM_PROJECT_ID", "")        # proj_xxxxxxx
PIPEDREAM_CLIENT_ID = os.environ.get("PIPEDREAM_CLIENT_ID", "")
PIPEDREAM_CLIENT_SECRET = os.environ.get("PIPEDREAM_CLIENT_SECRET", "")
PIPEDREAM_ENVIRONMENT = os.environ.get("PIPEDREAM_ENVIRONMENT", "production")  # development|production
PIPEDREAM_API_BASE = "https://api.pipedream.com/v1"   # Connect REST + OAuth (v1)
PIPEDREAM_CONNECT_BASE = PIPEDREAM_API_BASE           # paths are project-scoped: /connect/{project_id}/...
# The vault credential holds a ~1h Pipedream token. Before a deploy/run we re-mint and
# rotate it if it is past, or within this margin of, its expiry — so a run long after
# Connect still authenticates (WS5).
TOKEN_REFRESH_MARGIN_SECONDS = 300


def pipedream_mcp_url(external_user_id, app, project=None, environment=None):
    """The per-customer, per-app Pipedream MCP server URL. All routing rides in the
    query string (MA injects only the bearer token, matched to the server by URL); one
    app per URL. ``projectId``, ``environment``, ``externalUserId``, ``app`` are all
    required."""
    q = urllib.parse.urlencode({
        "projectId": project or PIPEDREAM_PROJECT,
        "environment": environment or PIPEDREAM_ENVIRONMENT,
        "externalUserId": external_user_id or "",
        "app": app,
    })
    return f"{PIPEDREAM_MCP_BASE}?{q}"


def pipedream_mcp_servers(external_user_id, app_slugs, staged_slugs=None):
    """Build the LIVE agent's connection block for one customer: one ``mcp_servers``
    entry per app (one app per URL) plus the matching ``mcp_toolset`` entries. Returns
    ``(servers, toolsets)``.

    ``staged_slugs`` (Spec 8 "stage, don't execute") is the set of app slugs the user
    opted into staging at deploy: that app's toolset gets ``require_approval`` so the
    platform HOLDS its outbound actions (Trace captures + stages them) instead of
    auto-executing. Any slug not in the set keeps ``always_allow``. ``None`` (the
    default) stages nothing — every toolset is ``always_allow``, the prior behavior."""
    staged = set(staged_slugs or ())
    servers, toolsets = [], []
    for slug in app_slugs:
        name = f"{PIPEDREAM_MCP_NAME}_{slug}"
        policy = APPROVAL_PERMISSION_POLICY if slug in staged else LIVE_PERMISSION_POLICY
        servers.append({"name": name, "type": "url",
                        "url": pipedream_mcp_url(external_user_id, slug)})
        toolsets.append({"type": MCP_TOOLSET_TYPE, "mcp_server_name": name,
                         "default_config": {"permission_policy": {"type": policy}}})
    return servers, toolsets

# Observed-app substring -> Pipedream app {slug, label}. Domain-neutral; same match
# idiom as APP_MCP_TABLE. Apps with no row are surfaced as 'unmapped', never guessed.
# The exact Pipedream slugs are placeholders — confirm against the Pipedream app registry.
CONNECT_APP_TABLE = {
    "gmail":         {"slug": "gmail",           "label": "Gmail"},
    "google sheets": {"slug": "google_sheets",   "label": "Google Sheets"},
    "sheets":        {"slug": "google_sheets",   "label": "Google Sheets"},
    "google drive":  {"slug": "google_drive",    "label": "Google Drive"},
    "drive":         {"slug": "google_drive",    "label": "Google Drive"},
    "google docs":   {"slug": "google_docs",     "label": "Google Docs"},
    "docs":          {"slug": "google_docs",     "label": "Google Docs"},
    "slack":         {"slug": "slack",           "label": "Slack"},
    "outlook":       {"slug": "outlook",         "label": "Outlook"},
    "calendar":      {"slug": "google_calendar", "label": "Google Calendar"},
}

# Worker-facing expectation copy shown BEFORE Google's OAuth window (spec step 2).
CONNECT_EXPECTATION_COPY = ("Google will ask you to pick your account, then approve "
                            "access — two taps. You won't create any new accounts.")

# Canonical, domain-neutral working rule the generator must always include; appended
# deterministically if the model omits it (so the agent always matches existing work).
MATCH_EXAMPLES_RULE = ("Before producing the work product, look at the examples of this "
                       "work already present in the live environment and match their format.")

# Canonical anti-duplication working rule (Spec 7). The PRINCIPLE is constant and
# domain-neutral; the config engine is told (config_prompt) to also emit a version that
# NAMES this workflow's concrete outputs. This constant is the deterministic backstop,
# appended only if the model omitted any anti-duplication rule, so every agent has one.
# Worded to avoid the match/existing/example words (so it never trips _has_match_rule).
NO_DUPLICATION_RULE = ("Never repeat an action this workflow already completed: before "
                       "sending, creating, or changing anything outside the workflow, "
                       "check the live environment for evidence the same action was already "
                       "done for this item (for example a prior message in Sent, or an entry "
                       "already recorded for it), and skip it if so.")

# --- Console (manual-evaluation mode) --------------------------------------
# The run result is a POINTER to the live session, read by the user in the Console.
# The exact deep-link path to a single session is not finalized in the beta docs,
# so we link to the Managed Agents area and always surface the session id + this
# hint; the user opens the agent's Sessions tab to read the run.
CONSOLE_BASE = "https://platform.claude.com"
CONSOLE_SESSIONS_HINT = "Console -> Managed Agents -> Sessions"


def console_session_url(session_id=None, agent_id=None):
    """Best-effort Console link for a run. Returns the Managed Agents area; the
    session id is shown alongside so the user can locate it under Sessions."""
    return f"{CONSOLE_BASE}/managed-agents"

# Environment object (required; created once, its id passed on every sessions.create).
# Networking is unrestricted so the LIVE agent can reach the Pipedream MCP server.
ENVIRONMENT_DEFAULTS = {
    "name": "trace-test-env",
    "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
}

# --- P4 — observed application identity -> MCP server entry -----------------
# Keys are lowercased application-identity substrings matched against the app/window
# seen in the bundle's evidence events. General + editable; add rows as needed.
# Apps with no row here are surfaced as unmapped (a YAML comment), never guessed.
APP_MCP_TABLE = {
    "gmail":         {"name": "gmail",        "url": "https://mcp.anthropic.com/gmail"},
    "google sheets": {"name": "google_drive", "url": "https://mcp.anthropic.com/drive"},
    "sheets":        {"name": "google_drive", "url": "https://mcp.anthropic.com/drive"},
    "google drive":  {"name": "google_drive", "url": "https://mcp.anthropic.com/drive"},
    "drive":         {"name": "google_drive", "url": "https://mcp.anthropic.com/drive"},
    "google docs":   {"name": "google_drive", "url": "https://mcp.anthropic.com/drive"},
    "slack":         {"name": "slack",        "url": "https://mcp.anthropic.com/slack"},
    "outlook":       {"name": "outlook",      "url": "https://mcp.anthropic.com/outlook"},
    "calendar":      {"name": "google_calendar", "url": "https://mcp.anthropic.com/calendar"},
}

# --- framing templates (parameterized by workflow name + mount path) -------
FRAMING_TEST = (
    'You are an automation agent. Your workflow is "{workflow}", defined by the spec '
    'below. The files the spec references are mounted in your workspace at {mount}. '
    'Those mounted files are READ-ONLY: write any modified or output files to a NEW '
    'path under {outputs}/ (do not attempt to modify the mounted originals). '
    'Write EVERY file you create or modify under {outputs}/ — only files saved there '
    'can be retrieved after the run; the mounted inputs cannot. '
    'When you receive a trigger for this workflow, carry it out per the spec. '
    'You have NO external connections in this test run: produce any outbound action '
    '(an email, a chat message, a post) as TEXT in your response instead of sending it.'
)
FRAMING_LIVE = (
    'You are an automation agent. Your workflow is "{workflow}", defined by the spec '
    'below. The files the spec references are mounted in your workspace at {mount}. '
    'Those mounted files are READ-ONLY: write any modified or output files to a NEW '
    'path under {outputs}/. '
    'When you receive a trigger for this workflow, carry it out per the spec. '
    'Real connections to the services below are available: perform outbound actions '
    'through those connected tools (do not just describe them).'
)
