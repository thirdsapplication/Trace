-- 015: first-run state per account (PALZA_REDESIGN §3).
--
-- A new account is walked through Welcome (04) → Privacy & terms (05) → the setup
-- wizard; a returning account lands on Home and never re-enters the wizard unless it
-- asks to. That needs two facts to survive a sign-out, so they live on the profile
-- rather than in the browser — the same person on a second machine is not a new user.
--
-- Both are nullable and read defensively (registry.user_flags_get tolerates their
-- absence), so the app behaves correctly whether or not this migration has been applied:
-- without it every account simply looks like a first run.

ALTER TABLE profiles ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMPTZ;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS first_run_completed_at TIMESTAMPTZ;
