-- Migration 003: Add missing columns to agents/agent_runs + create registry tables
-- (schedules, pending_actions, run_reviews, agent_messages, session_summaries)
-- Run this in Supabase SQL Editor.

-- ============================================================
-- 1. Add missing columns to existing tables
-- ============================================================

-- agents: columns the codebase relies on that the 001 migration didn't include
ALTER TABLE agents ADD COLUMN IF NOT EXISTS vault_ref TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS trigger TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS staged_apps JSONB DEFAULT '[]'::jsonb;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS deployment_id TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS memory_store_id TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS external_user_id TEXT;

-- agent_runs: columns the codebase relies on
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS details JSONB DEFAULT '[]'::jsonb;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS run_session_id TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS drun_id TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES profiles(id);

-- ============================================================
-- 2. Create missing tables
-- ============================================================

-- Schedules (one per agent, drives the cron scheduler)
CREATE TABLE IF NOT EXISTS schedules (
    agent_id   UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    owner_id   UUID NOT NULL REFERENCES profiles(id),
    cadence    JSONB NOT NULL,
    paused     BOOLEAN NOT NULL DEFAULT FALSE,
    last_run_at TIMESTAMPTZ
);

-- Pending actions (Spec 8: staged outbound actions awaiting approval)
CREATE TABLE IF NOT EXISTS pending_actions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id       UUID NOT NULL REFERENCES agents(id),
    owner_id       UUID NOT NULL REFERENCES profiles(id),
    run_session_id TEXT,
    app            TEXT,
    kind           TEXT,
    summary        TEXT,
    payload        JSONB DEFAULT '{}'::jsonb,
    content_hash   TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS pending_actions_owner_idx
    ON pending_actions (owner_id, status);

-- Run reviews (story review marks)
CREATE TABLE IF NOT EXISTS run_reviews (
    agent_id   UUID NOT NULL REFERENCES agents(id),
    owner_id   UUID NOT NULL REFERENCES profiles(id),
    run_key    TEXT NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, run_key)
);

-- Agent direct messages (Instagram-style DM threads)
CREATE TABLE IF NOT EXISTS agent_messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id   UUID NOT NULL REFERENCES agents(id),
    owner_id   UUID NOT NULL REFERENCES profiles(id),
    sender     TEXT NOT NULL,
    text       TEXT,
    read       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_messages_thread_idx
    ON agent_messages (owner_id, agent_id, created_at);

-- Session summary cache (activity view)
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY,
    summary    TEXT,
    status     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Agent decisions (human-in-the-loop, maps from pending_actions concept)
CREATE TABLE IF NOT EXISTS agent_decisions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id       UUID NOT NULL REFERENCES agents(id),
    owner_id       UUID NOT NULL REFERENCES profiles(id),
    run_id         UUID REFERENCES agent_runs(id),
    question       TEXT NOT NULL,
    answer         TEXT,
    context        JSONB DEFAULT '{}'::jsonb,
    reusable       BOOLEAN NOT NULL DEFAULT FALSE,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_decisions_owner_idx
    ON agent_decisions (owner_id, status);

-- Decision embeddings for RAG search
CREATE TABLE IF NOT EXISTS decision_embeddings (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id  UUID NOT NULL REFERENCES agent_decisions(id) ON DELETE CASCADE,
    embedding    vector(1536),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 3. RLS policies for new tables
-- ============================================================

ALTER TABLE schedules ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_embeddings ENABLE ROW LEVEL SECURITY;

-- Schedules: users see their own
CREATE POLICY "Users manage own schedules" ON schedules
    FOR ALL USING (owner_id = auth.uid());

-- Pending actions: users see their own
CREATE POLICY "Users manage own pending_actions" ON pending_actions
    FOR ALL USING (owner_id = auth.uid());

-- Run reviews: users see their own
CREATE POLICY "Users manage own run_reviews" ON run_reviews
    FOR ALL USING (owner_id = auth.uid());

-- Agent messages: users see their own
CREATE POLICY "Users manage own agent_messages" ON agent_messages
    FOR ALL USING (owner_id = auth.uid());

-- Session summaries: open (cached summaries, no sensitive data)
CREATE POLICY "Session summaries are readable" ON session_summaries
    FOR SELECT USING (true);
CREATE POLICY "Service role manages session_summaries" ON session_summaries
    FOR ALL USING (true);

-- Agent decisions: users see their own
CREATE POLICY "Users manage own agent_decisions" ON agent_decisions
    FOR ALL USING (owner_id = auth.uid());

-- Decision embeddings: service role only
CREATE POLICY "Service role manages decision_embeddings" ON decision_embeddings
    FOR ALL USING (true);
