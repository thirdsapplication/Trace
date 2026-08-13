-- 018: teach match_lessons about teams.
--
-- NOT YET WIRED UP, like 017. Apply whenever; nothing calls the new parameters until
-- registry.retrieve_lessons passes them.
--
-- 017 gives teams their TABLES. It does not make team lesson-sharing work, because the
-- retrieval RPC only knows two worlds: your own lessons, and everybody's. Its signature
-- takes a single `owner` plus an `allow_cross` boolean, so there is nowhere to say "these
-- four people are my team". That is why registry.retrieve_lessons still collapses
-- consume_scope='team' down to owner-only — not an oversight, a missing parameter.
--
-- This adds a third tier between the two: lessons from someone else IN YOUR TEAM.
--
-- Three deliberate choices, all reversible by editing this file before you apply it:
--
--   1. TEAM LESSONS DO NOT REQUIRE global_verified. That flag means "scrubbed and the
--      source consented to sharing with EVERYONE" (010). Requiring it for teammates
--      would mean you cannot share with your own colleagues without also publishing to
--      strangers, which is backwards. Team sharing is gated on the source's own
--      contribute scope instead.
--
--   2. CONSENT IS JOINED LIVE, not frozen onto the decision when it was written. If a
--      teammate later narrows their sharing to "keep to myself", their past lessons stop
--      being retrievable immediately. The alternative — stamping scope at write time —
--      makes revocation not apply retroactively, which is the wrong default for a
--      privacy control. (agent_decisions.share_scope exists and is vestigial; this is
--      the decision that keeps it that way.)
--
--   3. NO AGENT-KIND GATE ON THE TEAM TIER. Cross-owner retrieval demands the source
--      agent be similar-kind, because a stranger's lesson about a different job is
--      noise at best. Inside one team the context is already shared, so the kind gate
--      mostly filters out useful things. The situation threshold still applies, set
--      between the own-lesson bar and the stranger bar: more trusted than a stranger,
--      less automatically applicable than your own.
--
-- Run this in the Supabase SQL Editor.

-- DROP first, for two reasons, both of which this migration got wrong on the first
-- attempt and Postgres caught:
--
--   * agent_id is TEXT, not uuid. Managed Agents issues ids like "agent_0181qZ…";
--     012 retyped agents.id and every referencing column and recreated this function
--     to match. Writing the signature from 009 — which predates 012 — silently
--     reverted that and failed with
--         42P13 return type mismatch … returns text instead of uuid at column 4.
--
--   * Adding parameters CHANGES the signature, so CREATE OR REPLACE would not replace
--     anything — it would create a second, overloaded match_lessons alongside the
--     8-argument one, and an 8-argument call would then be ambiguous.
DROP FUNCTION IF EXISTS match_lessons(vector, uuid, vector, boolean, float, float, float, int);

CREATE OR REPLACE FUNCTION match_lessons(
    situation_embedding   vector(1024),
    owner                 uuid,
    agent_kind_embedding  vector(1024) DEFAULT NULL,
    allow_cross           boolean DEFAULT false,
    min_situation         float DEFAULT 0.5,
    min_cross_situation   float DEFAULT 0.65,
    min_agent_kind        float DEFAULT 0.6,
    match_count           int DEFAULT 5,
    team_owners           uuid[] DEFAULT NULL,     -- NEW: the caller's teammates
    min_team_situation    float DEFAULT 0.58       -- NEW: between own and cross
)
RETURNS TABLE (decision_id uuid, generalized_lesson text, owner_id uuid,
               agent_id text, similarity float, tier text)
LANGUAGE sql
STABLE
SECURITY DEFINER
AS $$
    SELECT d.id,
           d.generalized_lesson,
           d.owner_id,
           d.agent_id,
           1 - (e.embedding <=> situation_embedding) AS similarity,
           CASE WHEN d.owner_id = owner THEN 'own'
                WHEN d.owner_id = ANY(COALESCE(team_owners, ARRAY[]::uuid[])) THEN 'team'
                ELSE 'cross' END AS tier
    FROM decision_embeddings e
    JOIN agent_decisions d ON d.id = e.decision_id
    LEFT JOIN agent_embeddings ae ON ae.agent_id = d.agent_id
    LEFT JOIN profiles src ON src.id = d.owner_id          -- live consent, see note 2
    WHERE d.reusable = true
      AND d.generalized_lesson IS NOT NULL
      AND (
        (d.owner_id = owner
         AND 1 - (e.embedding <=> situation_embedding) >= min_situation)
        OR
        -- NEW: a teammate's lesson. No global_verified, no kind gate; the source must
        -- still have chosen to contribute at team scope or wider.
        (d.owner_id <> owner
         AND d.owner_id = ANY(COALESCE(team_owners, ARRAY[]::uuid[]))
         AND COALESCE(src.lesson_contribute_scope, 'global') IN ('team', 'global')
         AND 1 - (e.embedding <=> situation_embedding) >= min_team_situation)
        OR
        (allow_cross AND d.owner_id <> owner
         AND NOT (d.owner_id = ANY(COALESCE(team_owners, ARRAY[]::uuid[])))
         AND d.global_verified = true            -- scrub-verified + source consented (010)
         AND 1 - (e.embedding <=> situation_embedding) >= min_cross_situation
         AND agent_kind_embedding IS NOT NULL
         AND ae.embedding IS NOT NULL
         AND 1 - (ae.embedding <=> agent_kind_embedding) >= min_agent_kind)
      )
    ORDER BY e.embedding <=> situation_embedding
    LIMIT match_count;
$$;

-- Backwards compatible on purpose: both new parameters default, and with team_owners
-- NULL this behaves exactly as 012's version did. Callers can adopt it one at a time,
-- and an unapplied 018 simply means team scope keeps collapsing to owner-only.
--
-- If you ever write another migration against this function, copy the signature from
-- the LATEST one that touched it (012, then this), never from 009. That is exactly the
-- mistake that made the first version of this file fail.
