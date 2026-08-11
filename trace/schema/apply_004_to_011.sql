-- ============================================================
-- Trace: apply migrations 004..011 in one shot (idempotent).
-- Generated from trace/schema/004..011. 003 already applied.
-- Run in Supabase Dashboard > SQL Editor.
-- ============================================================


-- ========== 004_pending_rationale.sql ==========
-- 004: proposal detail quality (Spec 8 human-in-the-loop, gap #1)
-- Adds a first-class `rationale` to staged proposals so a human sees WHY an action
-- is proposed (citing the evidence the agent read), not just WHAT it would do.
-- Kept separate from `payload` so the executable action fields stay pure.

ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS rationale TEXT;

-- ========== 005_decision_memory.sql ==========
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

-- ========== 006_decision_embeddings_voyage.sql ==========
-- 006: decision embeddings for Voyage (Gap #3 B3). voyage-3.5 outputs 1024 dims, so the
-- decision_embeddings vector column (created at 1536 in 003) moves to 1024. Safe on an
-- empty table (the RAG layer is greenfield). Adds the cosine ivfflat index used by
-- retrieval (B4). See docs/decision_memory_design.md.

ALTER TABLE decision_embeddings ALTER COLUMN embedding TYPE vector(1024);

CREATE INDEX IF NOT EXISTS decision_embeddings_ivf
    ON decision_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ========== 007_match_decisions.sql ==========
-- 007: decision retrieval RPC (Gap #3 B4). Cosine similarity search over
-- decision_embeddings joined to agent_decisions, returning ONLY the de-identified
-- generalized lesson (never raw question/answer/context) — the single crossing point
-- described in docs/decision_memory_design.md §5.
--
-- Owner-scoped (T0/T1): the caller passes the owner id and only that owner's reusable
-- lessons are searched. Cross-user tiers (T2/T3) will extend this with a scope arg + the
-- DECISION_SHARE_GLOBAL gate in B5. SECURITY DEFINER so it can read the service-role-only
-- decision_embeddings table; it still restricts rows to the passed owner.

CREATE OR REPLACE FUNCTION match_decisions(
    query_embedding vector(1024),
    owner uuid,
    match_count int DEFAULT 3,
    min_similarity float DEFAULT 0.5
)
RETURNS TABLE (decision_id uuid, generalized_lesson text, similarity float)
LANGUAGE sql
STABLE
SECURITY DEFINER
AS $$
    SELECT d.id,
           d.generalized_lesson,
           1 - (e.embedding <=> query_embedding) AS similarity
    FROM decision_embeddings e
    JOIN agent_decisions d ON d.id = e.decision_id
    WHERE d.reusable = true
      AND d.generalized_lesson IS NOT NULL
      AND d.owner_id = owner
      AND 1 - (e.embedding <=> query_embedding) >= min_similarity
    ORDER BY e.embedding <=> query_embedding
    LIMIT match_count;
$$;

-- ========== 008_fleet_learning.sql ==========
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

-- ========== 009_match_lessons.sql ==========
-- 009: Phase B double-gated lesson retrieval (Gap #3 B5b). Extends match_decisions (007)
-- with scope + the agent-kind gate: a lesson from a DIFFERENT owner is only returned when
-- its SOURCE AGENT is similar-kind to the querying agent (agent_embeddings similarity),
-- with a HIGHER situation bar than own-owner lessons. Returns each hit's tier so the caller
-- (B5c) can apply own lessons autonomously but gate cross-user lessons behind promotion.
-- SECURITY DEFINER to read the service-role-only embedding tables; still returns only the
-- de-identified generalized_lesson plus source ids. See docs/decision_memory_design.md §9.

-- The global-share gate column the function body references. Also (re)added in 010; kept
-- here so this migration's CREATE FUNCTION validates regardless of apply order.
ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS global_verified BOOLEAN NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION match_lessons(
    situation_embedding   vector(1024),
    owner                 uuid,
    agent_kind_embedding  vector(1024) DEFAULT NULL,
    allow_cross           boolean DEFAULT false,
    min_situation         float DEFAULT 0.5,
    min_cross_situation   float DEFAULT 0.65,
    min_agent_kind        float DEFAULT 0.6,
    match_count           int DEFAULT 5
)
RETURNS TABLE (decision_id uuid, generalized_lesson text, owner_id uuid,
               agent_id uuid, similarity float, tier text)
LANGUAGE sql
STABLE
SECURITY DEFINER
AS $$
    SELECT d.id,
           d.generalized_lesson,
           d.owner_id,
           d.agent_id,
           1 - (e.embedding <=> situation_embedding) AS similarity,
           CASE WHEN d.owner_id = owner THEN 'own' ELSE 'cross' END AS tier
    FROM decision_embeddings e
    JOIN agent_decisions d ON d.id = e.decision_id
    LEFT JOIN agent_embeddings ae ON ae.agent_id = d.agent_id
    WHERE d.reusable = true
      AND d.generalized_lesson IS NOT NULL
      AND (
        (d.owner_id = owner
         AND 1 - (e.embedding <=> situation_embedding) >= min_situation)
        OR
        (allow_cross AND d.owner_id <> owner
         AND d.global_verified = true            -- scrub-verified + source consented (010)
         AND 1 - (e.embedding <=> situation_embedding) >= min_cross_situation
         AND agent_kind_embedding IS NOT NULL
         AND ae.embedding IS NOT NULL
         AND 1 - (ae.embedding <=> agent_kind_embedding) >= min_agent_kind)
      )
    ORDER BY e.embedding <=> situation_embedding
    LIMIT match_count;
$$;

-- ========== 010_lesson_global_gate.sql ==========
-- 010: T3 scrub-verify gate (Gap #3 B5d). A lesson may only be retrieved by ANOTHER user
-- once it has passed a second de-identification/scrub pass AND its source consented to
-- share globally. Both are folded into `global_verified` (set at learn time). Cross-user
-- retrieval (match_lessons, 009) requires it; own-owner retrieval never does.

ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS global_verified BOOLEAN NOT NULL DEFAULT false;

-- ========== 011_agent_groups.sql ==========
-- 011: agent group chat (color-coded "add your agents to a group chat" feature). See
-- memory/agent-groupchat-feature.md. A group is a named set of agent members owned by a
-- user; each agent carries a color for its message bubbles (auto-assigned, user-overridable).

ALTER TABLE agents ADD COLUMN IF NOT EXISTS color TEXT;   -- hex; NULL = derive default from id

CREATE TABLE IF NOT EXISTS chat_groups (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id   UUID NOT NULL REFERENCES profiles(id),
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_groups_owner_idx ON chat_groups (owner_id);

CREATE TABLE IF NOT EXISTS chat_group_members (
    group_id UUID NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, agent_id)
);

ALTER TABLE chat_groups        ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_group_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own chat_groups" ON chat_groups
    FOR ALL USING (owner_id = auth.uid());
CREATE POLICY "Users manage own chat_group_members" ON chat_group_members
    FOR ALL USING (group_id IN (SELECT id FROM chat_groups WHERE owner_id = auth.uid()));
