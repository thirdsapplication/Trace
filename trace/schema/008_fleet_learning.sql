-- 008: Phase B cross-user fleet learning (Gap #3 B5). See docs/decision_memory_design.md §9.
-- Decisions locked: contribute posture = opt-out ALL tiers (default 'global'); consent grain
-- = per-user default + per-agent override; cross-user lessons apply PROVISIONAL→CONFIRMED
-- per agent; provenance + retirement from day one; a lesson confirmed by >=5 distinct users
-- graduates to a global auto-trusted default.

-- --- consent (per-user default; per-agent override; NULL agent value = inherit user) -------
-- scope values: 'owner' | 'team' | 'global'. Opt-out posture -> defaults are 'global'.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS lesson_contribute_scope TEXT NOT NULL DEFAULT 'global';
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS lesson_consume_scope  TEXT NOT NULL DEFAULT 'global';
ALTER TABLE agents   ADD COLUMN IF NOT EXISTS lesson_contribute_scope TEXT;   -- NULL = inherit
ALTER TABLE agents   ADD COLUMN IF NOT EXISTS lesson_consume_scope  TEXT;      -- NULL = inherit

-- --- per-agent promotion of a cross-user lesson (the trust gate) ---------------------------
CREATE TABLE IF NOT EXISTS lesson_promotions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    owner_id     UUID NOT NULL REFERENCES profiles(id),
    decision_id  UUID NOT NULL REFERENCES agent_decisions(id) ON DELETE CASCADE,
    lesson       TEXT,                                  -- de-identified snapshot (for the card)
    status       TEXT NOT NULL DEFAULT 'provisional',   -- provisional | confirmed | retired
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ,
    UNIQUE (agent_id, decision_id)
);
CREATE INDEX IF NOT EXISTS lesson_promotions_agent_idx ON lesson_promotions (agent_id, status);
-- distinct confirming users per lesson (>=5 -> global auto-trust) is COUNT(DISTINCT owner_id)
CREATE INDEX IF NOT EXISTS lesson_promotions_decision_idx ON lesson_promotions (decision_id, status);

-- --- provenance: which lesson influenced which run ----------------------------------------
CREATE TABLE IF NOT EXISTS lesson_applications (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id       UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    owner_id       UUID NOT NULL REFERENCES profiles(id),
    decision_id    UUID NOT NULL REFERENCES agent_decisions(id) ON DELETE CASCADE,
    run_session_id TEXT,
    applied_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lesson_applications_decision_idx ON lesson_applications (decision_id);

-- --- agent-kind embeddings (double-gate: agent similarity × situation similarity) ---------
CREATE TABLE IF NOT EXISTS agent_embeddings (
    agent_id   UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    embedding  vector(1024),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_embeddings_ivf
    ON agent_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- --- RLS -----------------------------------------------------------------------------------
ALTER TABLE lesson_promotions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE lesson_applications ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_embeddings    ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own lesson_promotions"  ON lesson_promotions
    FOR ALL USING (owner_id = auth.uid());
CREATE POLICY "Users read own lesson_applications"  ON lesson_applications
    FOR SELECT USING (owner_id = auth.uid());
-- agent_embeddings + cross-user reads happen via SECURITY DEFINER RPCs (service role only)
CREATE POLICY "Service role manages agent_embeddings" ON agent_embeddings
    FOR ALL USING (auth.role() = 'service_role');
