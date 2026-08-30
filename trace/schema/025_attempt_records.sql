-- 025 - attempt records: a grade, a change type, and preconditions to route on.
--
-- The engine used to record a change with a pass/fail outcome. v6 grades A-F, because a
-- D and an F are both reverts and are not the same evidence: a D is a technique that
-- worked and underdelivered, an F is one that backfired, and collapsing them throws away
-- the distinction that matters most when another agent reads the record.
--
-- preconditions is prose the engine writes for the next engine to read. precondition_
-- vector is the countable half - store size, documents never referenced, share of short
-- sessions, repeated identical calls - which is what a matcher can actually filter on,
-- since prose cannot be compared against numbers.

ALTER TABLE agent_changes ADD COLUMN IF NOT EXISTS grade TEXT;
ALTER TABLE agent_changes ADD COLUMN IF NOT EXISTS improvement DOUBLE PRECISION;
ALTER TABLE agent_changes ADD COLUMN IF NOT EXISTS change_type TEXT;
ALTER TABLE agent_changes ADD COLUMN IF NOT EXISTS preconditions TEXT;
ALTER TABLE agent_changes ADD COLUMN IF NOT EXISTS precondition_vector JSONB
    NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE agent_changes ADD COLUMN IF NOT EXISTS why_it_worked_or_failed TEXT;

ALTER TABLE agent_changes DROP CONSTRAINT IF EXISTS agent_changes_grade_check;
ALTER TABLE agent_changes ADD CONSTRAINT agent_changes_grade_check
    CHECK (grade IS NULL OR grade IN ('A', 'B', 'C', 'D', 'F'));

-- The score is mean TOTAL TOKENS now, not tokens per unit of difficulty. The column
-- keeps its name so every existing reader carries on working; what it holds changed on
-- the cycle this migration ships with, and rows either side of that are not comparable.
COMMENT ON COLUMN learning_cycles.mean_performance IS
    'From migration 025: mean total tokens per valid session. Earlier rows hold mean '
    'tokens per unit of difficulty and are not comparable with later ones.';
