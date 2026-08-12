-- 017: everything Settings (18), Connected apps (26) and Team (30) need to exist.
--
-- NOT YET WIRED UP. This migration is written ahead of the UI so applying it and
-- building the screens are one sitting rather than two. Nothing in the app reads or
-- writes these columns yet, so it is safe to apply at any time — and safe NOT to
-- apply: no current code path touches them.
--
-- Run this in the Supabase SQL Editor.
--
-- Numbered 017 because 016_agent_owner_email.sql already exists. Do not add a second
-- file at any number that is already taken: a duplicate 014 cost a debugging session
-- on 2026-08-12, because "is 014 applied?" had no answer.

-- ── Account pane (18) ─────────────────────────────────────────────────────────
-- The frame shows "Sam Torres" and "sam@thirds.co · Thirds Foods". Only the email
-- exists today; profiles has no human name and no organisation.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS display_name TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS org_name     TEXT;

-- ── Notifications pane (18) ───────────────────────────────────────────────────
-- Three toggles, none of which had any storage. Defaults follow the frame, which
-- draws the first two on and the weekly digest off — the two that help you act are
-- opt-out, the one that is merely interesting is opt-in.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS notify_morning_brief BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS notify_agent_blocked BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS notify_weekly_digest BOOLEAN NOT NULL DEFAULT FALSE;

-- ── Appearance pane (18) ──────────────────────────────────────────────────────
-- 'system' follows the OS setting, which is what an unset preference should mean.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS theme TEXT NOT NULL DEFAULT 'system'
  CHECK (theme IN ('system', 'light', 'dark'));

-- ── Team (30) ─────────────────────────────────────────────────────────────────
-- These tables are the reason Team is the one frame marked BLOCKED. They also
-- un-stub lesson sharing: registry.retrieve_lessons currently treats
-- consume_scope='team' as owner-only precisely because there is no team to resolve.
CREATE TABLE IF NOT EXISTS teams (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       TEXT NOT NULL,
  created_by UUID REFERENCES profiles(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS team_members (
  team_id   UUID NOT NULL REFERENCES teams(id)    ON DELETE CASCADE,
  user_id   UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  role      TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
  joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (team_id, user_id)
);

-- Invites are rows, not emails-in-flight: frame 30 lists them with Resend and Cancel,
-- so a pending invite has to survive a page load. UNIQUE stops the same address being
-- invited twice to one team; accepted_at retains it as a record rather than deleting.
CREATE TABLE IF NOT EXISTS team_invites (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id     UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  email       TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
  invited_by  UUID REFERENCES profiles(id) ON DELETE SET NULL,
  invited_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  accepted_at TIMESTAMPTZ,
  UNIQUE (team_id, email)
);

-- "which team am I in" runs on every lesson read once team scope is live.
CREATE INDEX IF NOT EXISTS team_members_user_idx  ON team_members(user_id);
CREATE INDEX IF NOT EXISTS team_invites_email_idx ON team_invites(email);

-- ── NOT covered here, on purpose ──────────────────────────────────────────────
-- Connected apps (26) wants a last-used time per app and a Revoke action. Neither is
-- a migration: the connection record is the connection.json session artifact, not a
-- table, so both are changes in trace/agent/connect.py. Frame 26 also captions each
-- app with the agents needing it — connect.py already computes that while walking
-- bundles and then discards it collapsing into by_slug.
