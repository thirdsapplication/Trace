-- 004: proposal detail quality (Spec 8 human-in-the-loop, gap #1)
-- Adds a first-class `rationale` to staged proposals so a human sees WHY an action
-- is proposed (citing the evidence the agent read), not just WHAT it would do.
-- Kept separate from `payload` so the executable action fields stay pure.

ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS rationale TEXT;
