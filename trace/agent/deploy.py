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


def build_deployment_trigger(base_trigger, staged_slugs, rejected=None):
    """The deployment's initial user.message: the (optionally staging-prefixed) task, the
    uncertainty directive, anything the human has rejected, and the memory-maintenance
    directive. One composition point, reused whenever the deployment is recreated.

    The uncertainty directive belongs HERE, not only in ``run_once``. Deployment-backed
    agents are fired by the platform against this text, so while it was applied only on
    the run_once path they were never told they could pause and ask — the entire
    human-in-the-loop pause was inert for every real deployed agent. It showed up as
    agents staging "here's the problem, please handle it manually" emails: escalating by
    the only route they knew they had.

    ``rejected`` is what the human has turned down, so a recreated deployment stops
    proposing it. Deployments are immutable, so this only takes effect on recreation.
    """
    from . import runner                                   # local import: runner imports deploy helpers' siblings
    base = base_trigger or "on a new matching event"
    if staged_slugs:
        text = runner._staging_trigger(base, staged_slugs)
    else:
        text = f"Task: {base}"
    text = runner._uncertainty_trigger(text)
    if rejected:
        text = runner._rejected_preamble(rejected) + text
    return text + "\n\n" + config.MEMORY_UPDATE_DIRECTIVE


def deploy_live(bundle, live_envelope_dict, connection_record, client,
                trigger=None, schedule=None, staged_apps=None, connect_client=None):
    """Create the LIVE agent for one bundle over the bound connection, plus its
    per-agent MEMORY STORE (created-or-reused by name so lessons survive redeploys)
    and a platform DEPLOYMENT whose initial message set carries the task + the
    memory-update directive (+ the staging directive when apps are staged). The
    deployment is created MANUAL (no schedule) — the dashboard cadence attaches a
    cron schedule afterwards. Returns a ``live_record``. The customer's vault rides
    on the deployment (injected into every run session). Memory-store and deployment
    creation are NOT wrapped: a platform rejection propagates so the deploy job
    surfaces the real API error instead of a phantom "live" (the always_ask lesson).
    ``staged_apps`` (Spec 8) marks which app slugs are staged for approval.

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

    live_envelope_dict = finalize_live_envelope(live_envelope_dict, connection_record, staged_apps)

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

    agent = client.create_or_reuse_agent(live_envelope_dict)
    agent_id = agent.get("id") if isinstance(agent, dict) else agent
    environment_id = client.create_environment()

    store = client.create_or_reuse_memory_store(
        name=config.memory_store_name(agent_id, external_user_id),
        description=config.MEMORY_STORE_DESCRIPTION)
    memory_store_id = store.get("id") if isinstance(store, dict) else store

    base_trigger = trigger or (bundle.get("trigger") or "on a new matching event")
    trigger_text = build_deployment_trigger(base_trigger, staged_apps)
    # A cron ``schedule`` at deploy (the founder Test UI's "on a schedule" mode) is
    # honored as the deployment's cron; the end-user flow deploys manual (schedule=None)
    # and attaches a cadence afterwards from the dashboard.
    cron = ({"expression": schedule, "timezone": config.TRACE_SCHEDULE_TZ}
            if schedule else None)
    deployment_id = client.create_deployment(
        name=f"trace-{bundle.get('id')}",
        agent_id=agent_id, environment_id=environment_id,
        trigger_text=trigger_text,
        schedule=cron,
        resources=[config.memory_resource(memory_store_id)],
        vault_ids=[vault_ref] if vault_ref else None,
        metadata={"trace_bundle_id": str(bundle.get("id"))})

    start = {"mode": "deployment-schedule" if schedule else "deployment-manual",
             "value": schedule or base_trigger}
    return {
        "bundle_id": bundle.get("id"),
        "agent_id": agent_id,
        "session_id": None,        # deployment runs create their sessions; kept for record-shape compat
        "deployment_id": deployment_id,
        "memory_store_id": memory_store_id,
        "memory_store_reused": bool(isinstance(store, dict) and store.get("reused")),
        "vault_ref": vault_ref,
        "external_user_id": external_user_id,
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
