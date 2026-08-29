-- 023: the self-improvement engine's change log and document snapshots.
--
-- The engine used to rewrite an agent's memory every N sessions and hope. It had one
-- feedback signal — average tokens per session — and no way to tell which of its edits
-- moved that number, no way to undo one that made things worse, and no memory of what it
-- tried four cycles ago. These two tables are what turn that into a loop that can learn
-- from its own results.
--
-- agent_changes is the log. ONE change per cycle, so that when the number moves it is
-- known what moved it; the model READS this table and never writes it, because a model
-- that can edit its own track record does not have one. It stores NUMBERS, not verdicts:
-- a change that missed the bar by a point on a noisy batch and one that made performance
-- fifteen percent worse are different evidence, and a pass/fail flag would destroy the
-- difference. outcome is derived from those numbers when the following cycle judges the
-- change, which is why it is nullable — a row is written when a change is APPLIED and
-- settled a cycle later.
--
-- agent_doc_snapshots is what makes revert real. A revert is a FILE RESTORE, never a
-- model call: a model asked to undo its own change paraphrases it back into existence.
-- This is only sound because every document the engine may write has exactly one author
-- (see config.UNWRITABLE_DOCS) — the agent writes live state and the engine writes
-- everything else, so nothing has touched these bytes in between.
--
-- technique is written by the analyst in domain-general terms and is the ONLY field that
-- crosses to another customer's agent, through peer_bundles.improvement_log. summary is
-- for humans reading this log and goes through the same scrub gate before it travels.
-- docs and the snapshot payloads NEVER cross: they are that customer's memory contents.
--
-- agent_id is TEXT (platform string), not UUID — see 012.

CREATE TABLE IF NOT EXISTS agent_changes (
    id             BIGSERIAL PRIMARY KEY,
    agent_id       TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    owner_id       UUID REFERENCES profiles(id),
    change_id      TEXT NOT NULL,                       -- the analyst's short id
    summary        TEXT,                                -- one human-readable sentence
    technique      TEXT,                                -- the mechanism, domain-general
    rationale      TEXT,                                -- expected saving; diagnostic only
    paths          JSONB NOT NULL DEFAULT '[]'::jsonb,  -- documents this change touched
    peer_informed  TEXT,                                -- "<peer_ref>:<change_id>", or null
    retry_of       TEXT,                                -- a change_id from retryable
    baseline       DOUBLE PRECISION,                    -- mean P before it was applied
    observed       DOUBLE PRECISION,                    -- mean P in the batch after
    threshold      DOUBLE PRECISION,                    -- the bar it was measured against
    valid_rate     DOUBLE PRECISION,                    -- of the batch that judged it
    base_valid_rate DOUBLE PRECISION,
    outcome        TEXT,                                -- kept | reverted | banned | null
    reason         TEXT,                                -- why, in the judging cycle's words
    applied_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    judged_at      TIMESTAMPTZ,
    CONSTRAINT agent_changes_outcome_check
        CHECK (outcome IS NULL OR outcome IN ('kept', 'reverted', 'banned'))
);

-- The analyst reads this agent's log newest-first every cycle.
CREATE INDEX IF NOT EXISTS agent_changes_agent_idx
    ON agent_changes (agent_id, applied_at DESC);
-- "is there a change awaiting judgment?" is asked once per cycle.
CREATE INDEX IF NOT EXISTS agent_changes_pending_idx
    ON agent_changes (agent_id) WHERE outcome IS NULL;

CREATE TABLE IF NOT EXISTS agent_doc_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    change_id   TEXT NOT NULL,
    docs        JSONB NOT NULL DEFAULT '{}'::jsonb,     -- {path: content} BEFORE the change
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_doc_snapshots_change_idx
    ON agent_doc_snapshots (agent_id, change_id);

-- Peer improvement logs ride with the bundle they belong to. A transcript is untested
-- behaviour; a log entry is a result somebody already paid to obtain, and the NEGATIVE
-- results are the valuable half — a change that looks obviously good and is not is
-- exactly what no agent avoids independently, because it looks obviously good to all of
-- them. Summaries and techniques only; never docs, never snapshots.
ALTER TABLE peer_bundles ADD COLUMN IF NOT EXISTS improvement_log JSONB NOT NULL
    DEFAULT '[]'::jsonb;

-- The measurement itself rides on the cycle row. In observe-only mode (calibration, and
-- after convergence) no change is ever applied, so this is the ONLY record that a cycle
-- measured anything — and the spread of these figures across quiet batches is exactly what
-- MIN_IMPROVEMENT has to be set above.
ALTER TABLE learning_cycles ADD COLUMN IF NOT EXISTS mean_performance DOUBLE PRECISION;
ALTER TABLE learning_cycles ADD COLUMN IF NOT EXISTS valid_rate DOUBLE PRECISION;

ALTER TABLE agent_changes ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_doc_snapshots ENABLE ROW LEVEL SECURITY;
-- Written and read via the service role, like learning_cycles (021) and peer_bundles
-- (022). CREATE POLICY takes no IF NOT EXISTS, so re-applying this file fails with
--     42710  policy "..." already exists
-- and that error is SAFE to see: everything above it is idempotent, so nothing is left
-- half-applied. 021 has the identical hazard and an operator read the same 42710 as a
-- failed migration, which cost a support round.
CREATE POLICY "Service role manages agent_changes" ON agent_changes
    FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "Service role manages agent_doc_snapshots" ON agent_doc_snapshots
    FOR ALL USING (auth.role() = 'service_role');
