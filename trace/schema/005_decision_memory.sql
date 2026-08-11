-- 005: decision memory storage (Gap #3 B2). agent_decisions already exists (003);
-- this adds the fields the uncertainty/fleet-learning loop needs. See
-- docs/decision_memory_design.md.
--   run_session_id     — link a decision to the platform session that produced it
--                        (mirrors pending_actions; also the dedup key)
--   tags               — retrieval tags {app, action_kind, workflow_type, bundle_id}
--   generalized_lesson — de-identified reusable rule (populated in B3; the ONLY field
--                        allowed to cross user boundaries)
--   share_scope        — 'agent' | 'owner' | 'team' | 'global' (default owner = T1)

ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS run_session_id TEXT;
ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '{}'::jsonb;
ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS generalized_lesson TEXT;
ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS share_scope TEXT NOT NULL DEFAULT 'owner';
