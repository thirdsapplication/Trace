"""Phase 6 — live deployment (offline orchestration; all platform calls injected).

Creates and runs the LIVE-variant agent on Claude Managed Agents, connected to the
worker's real apps through the Phase-4 Pipedream connection: the bound vault is injected
into the live session (the single injection point, ``create_session(vault_ids=…)``), so
the agent acts ONLY on its own customer's accounts. The LIVE YAML already pins the
Pipedream toolset to approval-required, so every outbound write/send is HELD; this module
surfaces those held actions for one-tap approve/reject. Reads proceed.

This module imports no platform SDK — every external call goes through the injected
``client`` (a ManagedAgentsClient), so the whole flow runs offline against a stub. The
live-agent / vault-injection / trigger-schedule / approval shapes are external BETA;
confirm against current docs before live use.
"""

import copy
import datetime

from . import config


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _usable_app_slugs(connection_record):
    """The Pipedream app slugs this customer has a usable (verified) connection for."""
    return [slug for slug, a in ((connection_record or {}).get("apps") or {}).items()
            if a.get("usable")]


def finalize_live_envelope(live_envelope_dict, connection_record):
    """Bake the customer's per-app Pipedream MCP servers into the LIVE agent at create
    time (the reference's "one customer, one workflow -> bake the URL"). The config-gen
    template carries one nominal server; here it becomes one server per usable app, each
    URL carrying this customer's ``externalUserId``, plus the matching approval-required
    mcp_toolset entries. Returns a NEW dict (the template is not mutated)."""
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


def deploy_live(bundle, live_envelope_dict, connection_record, client,
                trigger=None, schedule=None):
    """Create + start the LIVE agent for one bundle over the bound connection. Returns a
    ``live_record``. The customer's vault is injected at session creation (isolation)."""
    vault_ref = (connection_record or {}).get("vault_ref")
    external_user_id = (connection_record or {}).get("external_user_id")

    live_envelope_dict = finalize_live_envelope(live_envelope_dict, connection_record)
    agent = client.create_or_reuse_agent(live_envelope_dict)
    agent_id = agent.get("id") if isinstance(agent, dict) else agent
    environment_id = client.create_environment()
    session_id = client.create_session(
        agent_id, environment_id, resources=[],
        title=f"Trace live — {bundle.get('id')}",
        vault_ids=[vault_ref] if vault_ref else None)        # bound vault injected here

    # Start mode is owned by Trace (no MA triggers API): a schedule feeds Trace's own
    # scheduler; a trigger is registered as a Pipedream deployed trigger. set_trigger only
    # records the descriptor — it makes no platform call and does not raise.
    start = {"mode": "schedule" if schedule else "trigger",
             "value": schedule or trigger or (bundle.get("trigger") or "on a new matching event")}
    try:
        client.set_trigger(agent_id, trigger=start["value"] if start["mode"] == "trigger" else None,
                           schedule=start["value"] if start["mode"] == "schedule" else None)
        status, error = "live", None
    except Exception as e:
        status, error = "errored", str(e)

    return {
        "bundle_id": bundle.get("id"),
        "agent_id": agent_id,
        "session_id": session_id,
        "vault_ref": vault_ref,
        "external_user_id": external_user_id,
        "start": start,
        "status": status,
        "error": error,
        "held_actions": list_held(client, session_id),
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
