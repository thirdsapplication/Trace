"""Every column must be mapped, or explicitly declared unexposed.

Five bugs this month shared one shape: a column written faithfully to Supabase and
dropped on the way back. color, owner_email and both consent scopes on agents;
ended_at on runs; status on agents twice — once on the write map and once on the
read map, the second of which would have made the first inert.

They survive because the two paths disagree. File mode returns the raw dict, so the
whole suite passes; only Supabase — the path production actually uses — goes through
a mapper that can drop a field. No behavioural test can see it.

So this asserts on the mappers directly, against a list of the columns each table
really has. When someone adds a column they must add it here too, and at that moment
they are made to choose: map it, or say out loud that it is not exposed. Either is
fine. Silently dropping it is what stops being possible.

Refresh the lists with `python -m trace.tests.test_mapping_completeness --dump`
against a configured Supabase.
"""

import inspect
import unittest

from trace.ui import registry

# The real columns, as of migration 021.
AGENTS = {
    "id", "session_id", "owner_id", "bundle_id", "name", "description", "live_yaml",
    "grounding", "trigger", "vault_ref", "staged_apps", "deployment_id",
    "memory_store_id", "external_user_id", "owner_email", "color", "status",
    "deployed_at", "created_at", "interview", "team_id",
    "lesson_contribute_scope", "lesson_consume_scope",
    # 021 — approval became a whole-agent setting, and an agent gained a second
    # deployment (staging + execution, one unpaused). `deployment_id` still names the
    # ACTIVE one so every pre-existing reader keeps working; `deployments` holds the
    # pair. `staged_apps` stays above, read-only, so pre-021 rows remain interpretable.
    "approval_required", "deployments", "deployment_mode",
}
AGENT_RUNS = {
    "id", "agent_id", "owner_id", "run_session_id", "drun_id", "started_at",
    "ended_at", "status", "summary", "details", "error", "actions_taken",
    "actions_held", "approvals", "trigger_type", "kind",
}

# Identity and bookkeeping the caller shape renames or does not need.
STRUCTURAL = {"id", "owner_id", "agent_id", "created_at", "session_id"}


def _named_in(fn):
    """Column names the mapper actually mentions."""
    src = inspect.getsource(fn)
    return {c for c in (AGENTS | AGENT_RUNS) if f'"{c}"' in src}


class MappingCompletenessTest(unittest.TestCase):
    def test_every_agent_column_is_mapped_or_declared_unexposed(self):
        named = _named_in(registry._agent_to_caller)
        declared = set(registry.AGENT_COLUMNS_NOT_EXPOSED)
        missing = AGENTS - named - declared - STRUCTURAL
        self.assertEqual(missing, set(),
                         f"columns written to Supabase and never read back: {sorted(missing)}. "
                         "Map them in _agent_to_caller, or add them to "
                         "AGENT_COLUMNS_NOT_EXPOSED with a reason.")

    def test_every_run_column_is_mapped(self):
        named = _named_in(registry._run_to_caller)
        missing = AGENT_RUNS - named - STRUCTURAL
        self.assertEqual(missing, set(),
                         f"run columns never read back: {sorted(missing)}")

    def test_the_write_map_can_carry_the_deployment_pair(self):
        """A mode change writes three things; dropping any one desynchronises the row
        from the platform — an agent that says it stages while its execution
        deployment is the unpaused one."""
        sb = registry._agent_to_sb({"agent_id": "a", "owner_user_id": "u",
                                    "approval_required": True,
                                    "deployment_id": "dep_stg",
                                    "deployments": {"staging": "dep_stg",
                                                    "execution": "dep_exec"},
                                    "deployment_mode": "staging"})
        self.assertTrue(sb.get("approval_required"))
        self.assertEqual(sb.get("deployment_id"), "dep_stg")
        self.assertEqual(sb.get("deployments"),
                         {"staging": "dep_stg", "execution": "dep_exec"})
        self.assertEqual(sb.get("deployment_mode"), "staging")

    def test_the_write_map_can_carry_deploy_state(self):
        """The other direction. status and deployed_at were absent from the write
        allow-list, so a deploy recorded a deployment_id and nothing about going
        live — all 13 production agents read 'draft' forever."""
        sb = registry._agent_to_sb({"agent_id": "a", "owner_user_id": "u",
                                    "status": "live", "deployed_at": "2026-08-26T00:00:00Z"})
        self.assertEqual(sb.get("status"), "live")
        self.assertEqual(sb.get("deployed_at"), "2026-08-26T00:00:00Z")

    def test_unexposed_columns_are_real_columns(self):
        """A typo in the omission list would silently excuse a column that does not
        exist while the real one keeps being dropped."""
        self.assertTrue(set(registry.AGENT_COLUMNS_NOT_EXPOSED) <= AGENTS)


if __name__ == "__main__":
    import sys
    if "--dump" in sys.argv:                               # refresh against live Supabase
        from trace.ui import supabase_client as supa
        for table in ("agents", "agent_runs"):
            r = supa.get_admin().table(table).select("*").limit(1).execute()
            rows = getattr(r, "data", None) or []
            print(f"{table}:", sorted(rows[0].keys()) if rows else "no rows")
    else:
        unittest.main()
