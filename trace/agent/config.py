"""Stage 4 configuration — the editable knobs for the Agent Compiler + Test Deployer.

Everything platform- or policy-specific lives here so it is changed in one place.
Nothing here references a fixture, sender, tab, SKU, or bundle id; the app→MCP table
keys on application identity, not on any workflow's content.
"""

# --- platform pins (also mirrored in api_client; keep in sync) -------------
BETA_VERSION = "managed-agents-2026-04-01"
AGENT_TOOLSET_TYPE = "agent_toolset_20260401"

# --- pipeline defaults (overridable) ---------------------------------------
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_SESSIONS = 3                 # cost guard: sessions per test run
MOUNT_PATH = "/workspace/files"          # where the Test Deployer mounts artifacts
# Mounted files are READ-ONLY copies; the agent must write outputs to a new path.
# The framing makes this contractual and the Deployer retrieves session-scoped files.
OUTPUTS_PATH = "/workspace/outputs"

# P5 — safe default: every outbound-write capability requires approval. Never
# emit always_allow by default. Confirm the exact enum against current docs.
APPROVAL_PERMISSION_POLICY = "require_approval"

# Environment defaults for the sandbox (documented shape: name + config).
ENVIRONMENT_DEFAULTS = {
    "name": "trace-test-env",
    "config": {"type": "cloud", "networking": {"type": "limited"}},
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
