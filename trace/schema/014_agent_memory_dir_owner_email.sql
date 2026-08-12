-- 014: per-user memory stores + human-legible platform naming.
--
-- Memory stores are now ONE PER USER (config.memory_store_name keys on the
-- external_user_id), shared by all of that user's agents; each agent keeps its own
-- documents inside the store under a directory recorded here as agents.memory_dir
-- (config.agent_memory_dir — keyed on the platform agent NAME, which survives
-- redeploys and display renames). Runs read it back to build the per-run memory
-- directive; NULL means the agent predates the change and keeps its legacy
-- root-level documents in its old dedicated store.
--
-- agents.owner_email is the owner's account email captured at deploy, so every
-- session title, environment name, and deployment name Trace creates on Managed
-- Agents identifies the user at a glance (the old names repeated per user:
-- "trace-B1", "Trace group message", "trace-test-env"). NULL falls back to the
-- stable external_user_id at naming time.
--
-- Run this in the Supabase SQL Editor.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS memory_dir TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_email TEXT;
