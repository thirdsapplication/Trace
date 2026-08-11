-- 010: T3 scrub-verify gate (Gap #3 B5d). A lesson may only be retrieved by ANOTHER user
-- once it has passed a second de-identification/scrub pass AND its source consented to
-- share globally. Both are folded into `global_verified` (set at learn time). Cross-user
-- retrieval (match_lessons, 009) requires it; own-owner retrieval never does.

ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS global_verified BOOLEAN NOT NULL DEFAULT false;
