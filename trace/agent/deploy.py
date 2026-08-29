"""Phase 6 — live deployment (offline orchestration; all platform calls injected).

Creates and runs the LIVE-variant agent on Claude Managed Agents, connected to the
worker's real apps through the Phase-4 Pipedream connection: the bound vault is injected
into the live session (the single injection point, ``create_session(vault_ids=…)``), so
the agent acts ONLY on its own customer's accounts. Live toolsets default to
``always_allow`` (``config.LIVE_PERMISSION_POLICY``), so the agent acts autonomously; the
held-action plumbing below (``list_held`` / ``approve`` / ``reject``) stays available for
any toolset configured ``always_ask`` but does not trigger under the default.

This module imports no platform SDK — every external call goes through the injected
``client`` (a ManagedAgentsClient), so the whole flow runs offline against a stub. The
live-agent / vault-injection / trigger-schedule / approval shapes are external BETA;
confirm against current docs before live use.
"""

import copy
import datetime

from . import config, mcp_preflight


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _usable_app_slugs(connection_record):
    """The Pipedream app slugs this customer has a usable (verified) connection for."""
    return [slug for slug, a in ((connection_record or {}).get("apps") or {}).items()
            if a.get("usable")]


def finalize_live_envelope(live_envelope_dict, connection_record, staged_apps=None):
    """Bake the customer's per-app Pipedream MCP servers into the LIVE agent at create
    time (the reference's "one customer, one workflow -> bake the URL"). The config-gen
    template carries one nominal server; here it becomes one server per usable app, each
    URL carrying this customer's ``externalUserId``, plus the matching mcp_toolset entries.
    ``staged_apps`` (Spec 8) is recorded on the deploy record/agent row but does NOT
    change the platform policy: every toolset is ``always_allow``. Staging is prompt-level
    (per-connector approval would gate reads too) — the staged set drives a run-time
    directive (see runner). Returns a NEW dict (the template is not mutated)."""
    env = copy.deepcopy(live_envelope_dict)
    uid = (connection_record or {}).get("external_user_id")
    slugs = _usable_app_slugs(connection_record)
    if not slugs:                                   # no usable app -> leave the template as-is
        return env
    servers, toolsets = config.pipedream_mcp_servers(uid, slugs)
    env["mcp_servers"] = servers
    env["tools"] = [t for t in env.get("tools", []) if t.get("type") != config.MCP_TOOLSET_TYPE]
    env["tools"].extend(toolsets)
    return env


def finalize_system_prompt(live_envelope_dict, approval_required, trigger=None):
    """Bake the run-time contracts into the agent's ``system`` prompt at create time.

    The generated instructions (``configgen.assemble_system_text``) describe the
    workflow; the memory contract, the uncertainty contract and — only when the human
    requires approval — the staging contract are appended here, because this is the
    first point at which ``approval_required`` is known. Returns a NEW dict.
    """
    env = dict(live_envelope_dict)
    env["system"] = config.agent_system_prompt(env.get("system"), approval_required,
                                               trigger=trigger)
    return env


def create_deployments(client, *, agent_id, environment_id, owner, workflow,
                       approval_required, active_mode, schedule=None, resources=None,
                       vault_ids=None, metadata=None):
    """Create this agent's deployments and leave exactly ONE unpaused.

    An approval-required agent gets two — staging and execution — against the same
    agent, environment, memory store and cadence; an autonomous agent gets execution
    only. ``active_mode`` says which one runs; the other is created and immediately
    paused, so switching modes later is one pause plus one unpause rather than a
    rebuild (deployments are immutable, which is why the old single-deployment design
    could not honour the approval toggle on platform-fired runs at all).

    Returns ``{mode: deployment_id}``. Nothing here is wrapped in a try: a platform
    rejection must surface as a failed deploy, never as a phantom-live agent.
    """
    modes = config.deployment_modes(approval_required)
    if active_mode not in modes:
        active_mode = config.MODE_EXECUTION
    out = {}
    for mode in modes:
        did = client.create_deployment(
            name=config.deployment_name(owner, workflow, mode),
            agent_id=agent_id, environment_id=environment_id,
            trigger_text=config.deployment_initial_message(mode, approval_required),
            schedule=schedule,
            resources=list(resources) if resources else None,
            vault_ids=list(vault_ids) if vault_ids else None,
            metadata=dict(metadata) if metadata else None)
        out[mode] = did
        if mode != active_mode:
            # Best-effort: a deployment that fails to pause is a duplicate run, so say so
            # loudly, but a paused-state hiccup must not fail an otherwise good deploy.
            try:
                client.pause_deployment(did)
            except Exception as exc:                       # noqa: BLE001
                print(f"[deploy] could not pause the {mode} deployment {did}: "
                      f"{type(exc).__name__}: {exc} — it may run alongside "
                      f"{active_mode}", flush=True)
    return out


def set_active_mode(client, deployments, mode):
    """Switch which of an agent's deployments runs: unpause ``mode``, pause the rest.

    ``deployments`` is ``{mode: deployment_id}``. Unpause FIRST so a failure leaves the
    agent running something rather than nothing; then pause the others. Returns the
    active deployment id.
    """
    active = (deployments or {}).get(mode)
    if not active:
        raise ValueError(f"agent has no {mode!r} deployment")
    client.unpause_deployment(active)
    for m, did in (deployments or {}).items():
        if m != mode and did:
            client.pause_deployment(did)
    return active


def deploy_live(bundle, live_envelope_dict, connection_record, client,
                trigger=None, schedule=None, staged_apps=None, connect_client=None,
                approval_required=None):
    """Create the LIVE agent for one bundle over the bound connection, plus its own
    dedicated per-agent MEMORY STORE (created-or-reused by a name keyed on user +
    workflow, so its memory survives redeploys even though the platform agent id
    rotates; BOTH deployments attach the same store)
    and its DEPLOYMENTS — staging + execution when the human requires approval, execution
    alone when they do not, with exactly one unpaused. Each deployment's initial message
    is one line naming the mode; everything constant across runs (memory, uncertainty,
    and the staging contract for approval-required agents) rides in the agent's system
    prompt instead. The deployments, environment, and store are NAMED for the owner +
    workflow so an administrator can tell whose they are in the Console (runs inherit
    the name; the deployment name also carries its mode, so a Console reader can tell a
    staging run from an executing one). The
    deployment is created MANUAL (no schedule) — the dashboard cadence attaches a
    cron schedule afterwards. Returns a ``live_record``. The customer's vault rides
    on the deployment (injected into every run session). Memory-store and deployment
    creation are NOT wrapped: a platform rejection propagates so the deploy job
    surfaces the real API error instead of a phantom "live" (the always_ask lesson).
    ``approval_required`` decides both the system prompt (an autonomous agent carries
    no staging contract) and how many deployments are created: staging + execution when
    approval is required, execution alone when it is not. Legacy callers may pass
    ``staged_apps`` instead; a non-empty list means approval is required.

    ``connect_client`` (a ConnectClient, optional): when provided, two guards run
    before the agent is created:
      * TOOL PREFLIGHT — each per-app MCP server's served tools are listed (see
        ``mcp_preflight``) and recorded on the live record (``toolsets`` /
        ``preflight_warnings`` / ``preflight_errors``). Best-effort: recorded,
        never raised, never blocks.
      * BINDING CHECK — every baked ``mcp_servers`` URL must have a matching vault
        credential (``verify_bindings``); MA matches token→server by exact URL, so
        a miss means an agent that LOOKS deployed but reaches Pipedream with no
        bearer at run time. A confirmed miss RAISES (the phantom-live philosophy:
        surface the real error, never a green lie); a listing failure is recorded
        as ``preflight_errors["binding_check"]`` and does not block."""
    vault_ref = (connection_record or {}).get("vault_ref")
    external_user_id = (connection_record or {}).get("external_user_id")

    pf = {"toolsets": {}, "warnings": [], "errors": {}}
    if connect_client is not None and external_user_id:
        try:
            pf = mcp_preflight.preflight_apps(connect_client, external_user_id,
                                              _usable_app_slugs(connection_record))
        except Exception as exc:                     # never block a deploy on preflight
            pf = {"toolsets": {}, "warnings": [], "errors": {"*": str(exc)}}

    if approval_required is None:
        approval_required = bool([s for s in (staged_apps or []) if s])

    base_trigger = trigger or (bundle.get("trigger") or "on a new matching event")
    live_envelope_dict = finalize_live_envelope(live_envelope_dict, connection_record, staged_apps)
    live_envelope_dict = finalize_system_prompt(live_envelope_dict, approval_required,
                                                trigger=base_trigger)

    binding_check = {}
    verify = getattr(connect_client, "verify_bindings", None) if connect_client else None
    if callable(verify) and vault_ref:
        # the template's nominal base-URL server (no app baked) is not a real binding
        urls = [s.get("url") for s in live_envelope_dict.get("mcp_servers") or []
                if s.get("url") and s.get("url") != config.PIPEDREAM_MCP_BASE]
        if urls:
            try:
                binding_check = verify(vault_ref, urls)
            except Exception as exc:            # listing failed: warn, don't block
                pf["errors"]["binding_check"] = str(exc)
            else:
                missing = sorted(u for u, ok in binding_check.items() if not ok)
                if missing:
                    raise RuntimeError(
                        "vault has no credential for baked MCP server URL(s): "
                        + ", ".join(missing)
                        + " — the agent would deploy green but reach Pipedream "
                          "unauthenticated. Reconnect the app(s) and redeploy.")

    # WHO and WHAT, for every platform name: the account email when the app passed it
    # through on the connection record, else the worker's confirmed identity, else the
    # stable external user id — never anonymous.
    owner = config.owner_label((connection_record or {}).get("owner_email"),
                               (connection_record or {}).get("expected_identity"),
                               external_user_id)
    workflow = (live_envelope_dict.get("name") or bundle.get("name")
                or bundle.get("id") or "agent")

    # Owner-scope the platform agent's name BEFORE create_or_reuse_agent: it
    # deletes any same-named agent account-wide, and the envelope arrives carrying
    # the bare workflow name ("Inbox triage"), which repeats across customers —
    # without this, one customer's deploy could delete another's live agent. The
    # bare name stays in ``workflow`` above: it is the memory store's reuse key
    # and the UI display name; only the platform object is renamed.
    live_envelope_dict = dict(live_envelope_dict,
                              name=config.agent_platform_name(owner, workflow))

    agent = client.create_or_reuse_agent(live_envelope_dict)
    agent_id = agent.get("id") if isinstance(agent, dict) else agent
    environment_id = client.create_environment(config.environment_defaults(owner, workflow))

    # This agent's OWN store, keyed on (user, workflow name) — the workflow name is
    # the reuse key that survives redeploys; the platform agent id does not.
    store = client.create_or_reuse_memory_store(
        name=config.memory_store_name(external_user_id, workflow),
        description=config.memory_store_description(owner))
    memory_store_id = store.get("id") if isinstance(store, dict) else store

    # A cron ``schedule`` at deploy (the founder Test UI's "on a schedule" mode) is
    # honored as the deployment's cron; the end-user flow deploys manual (schedule=None)
    # and attaches a cadence afterwards from the dashboard. BOTH deployments carry the
    # same cadence — only the unpaused one fires.
    cron = ({"expression": schedule, "timezone": config.TRACE_SCHEDULE_TZ}
            if schedule else None)
    # A new approval-required agent starts in STAGING: the first thing its human sees
    # should be a proposal to approve, not an action already taken.
    active_mode = config.MODE_STAGING if approval_required else config.MODE_EXECUTION
    deployments = create_deployments(
        client, agent_id=agent_id, environment_id=environment_id,
        owner=owner, workflow=workflow,
        approval_required=approval_required, active_mode=active_mode,
        schedule=cron,
        resources=[config.memory_resource(memory_store_id)],
        vault_ids=[vault_ref] if vault_ref else None,
        metadata={"trace_bundle_id": str(bundle.get("id"))})
    # ``deployment_id`` stays the ACTIVE deployment so every existing reader (run-now,
    # run listing, the pause toggle, the active-agents query) keeps working unchanged.
    deployment_id = deployments[active_mode]

    start = {"mode": "deployment-schedule" if schedule else "deployment-manual",
             "value": schedule or base_trigger}
    return {
        "bundle_id": bundle.get("id"),
        "agent_id": agent_id,
        "session_id": None,        # deployment runs create their sessions; kept for record-shape compat
        "deployment_id": deployment_id,
        "deployments": dict(deployments),
        "deployment_mode": active_mode,
        "approval_required": bool(approval_required),
        "memory_store_id": memory_store_id,
        "memory_store_reused": bool(isinstance(store, dict) and store.get("reused")),
        "vault_ref": vault_ref,
        "external_user_id": external_user_id,
        "owner_email": (connection_record or {}).get("owner_email"),
        "start": start,
        "status": "live",
        "error": None,
        "staged_apps": list(staged_apps or []),
        "held_actions": [],
        "toolsets": pf["toolsets"],
        "preflight_warnings": pf["warnings"],
        "preflight_errors": pf["errors"],
        "binding_check": binding_check,
        "deployed_at": _now(),
        "isolation": "per-customer (vault scoped to external_user_id)",
    }


def list_held(client, session_id):
    """The outbound writes/sends the agent has proposed and is holding for approval."""
    try:
        return [{**a, "status": "held"} for a in client.list_pending_approvals(session_id)]
    except Exception:
        return []


def approve(client, session_id, action_id):
    """Commit one held outbound action (one-tap approval)."""
    client.approve_action(session_id, action_id)
    return {"action_id": action_id, "status": "approved"}


def reject(client, session_id, action_id):
    """Cancel one held outbound action."""
    client.reject_action(session_id, action_id)
    return {"action_id": action_id, "status": "rejected"}
