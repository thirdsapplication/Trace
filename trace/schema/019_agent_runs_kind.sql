-- 019: mark approval-execution runs so the activity view never re-extracts
-- "proposals" from a session that was told to act, not propose (that re-read
-- turned every approved action's completion report back into a fresh pending
-- card with live Approve buttons). NULL = ordinary run (all pre-existing rows).
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS kind TEXT;
