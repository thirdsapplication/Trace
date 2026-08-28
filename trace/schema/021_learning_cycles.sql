-- 021: Fable-driven learning cycles — one row per batch analysis.
--
-- Every LEARNING_CYCLE_SESSIONS completed sessions, one claude-fable-5 call reviews
-- the batch (compacted transcripts + per-session token cost) plus the agent's memory
-- documents and rewrites lessons_learned.md so future sessions get more accurate and
-- more token-efficient (trace/agent/learn.py). This table powers (a) the
-- sessions-since-last-cycle trigger (the newest row's created_at is the watermark),
-- (b) prior-cycle context in the next analysis prompt — the efficiency feedback
-- loop, and (c) token-efficiency trends over an agent's lifetime.
--
-- Per-session token counts live in session_stats JSONB rather than as new
-- agent_runs columns: file-mode runs are append-only JSONL with no update path,
-- and tokens are only known when the cycle fetches the session events — hours
-- after run_add wrote the row, so it is inherently a second write. If a runs-feed
-- dashboard later needs per-run columns, add them in a future migration and have
-- the cycle backfill them; SQL reaches per-session granularity here today via
-- jsonb_array_elements(session_stats).
--
-- agent_id is TEXT (platform string), not UUID — see 012.

CREATE TABLE IF NOT EXISTS learning_cycles (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id            TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    owner_id            UUID REFERENCES profiles(id),
    session_stats       JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{session_id,status,total_tokens,estimated}]
    total_input_tokens  BIGINT,
    total_output_tokens BIGINT,
    tokens_estimated    BOOLEAN NOT NULL DEFAULT FALSE,      -- any session lacked real usage
    analysis            TEXT,                                -- the model's findings summary
    docs_written        JSONB NOT NULL DEFAULT '[]'::jsonb,  -- paths write_memory confirmed
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS learning_cycles_agent_idx
    ON learning_cycles (agent_id, created_at DESC);

ALTER TABLE learning_cycles ENABLE ROW LEVEL SECURITY;
-- The registry writes and reads via the service role (supa.get_admin()), same as
-- agent_embeddings in 008; no user-facing surface reads this table directly.
CREATE POLICY "Service role manages learning_cycles" ON learning_cycles
    FOR ALL USING (auth.role() = 'service_role');
