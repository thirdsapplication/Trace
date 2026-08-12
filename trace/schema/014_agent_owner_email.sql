-- 014: human-legible platform naming — agents.owner_email.
--
-- Everything Trace creates on Managed Agents (session titles, environment names,
-- deployment names, memory-store names/descriptions) now identifies the owner at a
-- glance; the old names repeated identically across users ("trace-B1", "Trace group
-- message", "trace-test-env"). agents.owner_email is the owner's account email
-- captured at deploy so the run paths can name things without a per-run user
-- lookup; NULL (rows from before this migration) falls back to the stable
-- external_user_id at naming time — never anonymous.
--
-- Memory stores stay one-per-agent, but their names now key on the USER + the
-- platform WORKFLOW NAME (config.memory_store_name) instead of the platform agent
-- id: redeploys delete+recreate the agent under a NEW id, so id-keyed names minted
-- a fresh store on every redeploy and orphaned what the agent had learned. No
-- schema change is needed for that — agents.memory_store_id already holds the id.
--
-- Run this in the Supabase SQL Editor.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_email TEXT;
