-- 009: Phase B double-gated lesson retrieval (Gap #3 B5b). Extends match_decisions (007)
-- with scope + the agent-kind gate: a lesson from a DIFFERENT owner is only returned when
-- its SOURCE AGENT is similar-kind to the querying agent (agent_embeddings similarity),
-- with a HIGHER situation bar than own-owner lessons. Returns each hit's tier so the caller
-- (B5c) can apply own lessons autonomously but gate cross-user lessons behind promotion.
-- SECURITY DEFINER to read the service-role-only embedding tables; still returns only the
-- de-identified generalized_lesson plus source ids. See docs/decision_memory_design.md §9.

-- The global-share gate column the function body references. Also (re)added in 010; kept
-- here so this migration's CREATE FUNCTION validates regardless of apply order.
ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS global_verified BOOLEAN NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION match_lessons(
    situation_embedding   vector(1024),
    owner                 uuid,
    agent_kind_embedding  vector(1024) DEFAULT NULL,
    allow_cross           boolean DEFAULT false,
    min_situation         float DEFAULT 0.5,
    min_cross_situation   float DEFAULT 0.65,
    min_agent_kind        float DEFAULT 0.6,
    match_count           int DEFAULT 5
)
RETURNS TABLE (decision_id uuid, generalized_lesson text, owner_id uuid,
               agent_id uuid, similarity float, tier text)
LANGUAGE sql
STABLE
SECURITY DEFINER
AS $$
    SELECT d.id,
           d.generalized_lesson,
           d.owner_id,
           d.agent_id,
           1 - (e.embedding <=> situation_embedding) AS similarity,
           CASE WHEN d.owner_id = owner THEN 'own' ELSE 'cross' END AS tier
    FROM decision_embeddings e
    JOIN agent_decisions d ON d.id = e.decision_id
    LEFT JOIN agent_embeddings ae ON ae.agent_id = d.agent_id
    WHERE d.reusable = true
      AND d.generalized_lesson IS NOT NULL
      AND (
        (d.owner_id = owner
         AND 1 - (e.embedding <=> situation_embedding) >= min_situation)
        OR
        (allow_cross AND d.owner_id <> owner
         AND d.global_verified = true            -- scrub-verified + source consented (010)
         AND 1 - (e.embedding <=> situation_embedding) >= min_cross_situation
         AND agent_kind_embedding IS NOT NULL
         AND ae.embedding IS NOT NULL
         AND 1 - (ae.embedding <=> agent_kind_embedding) >= min_agent_kind)
      )
    ORDER BY e.embedding <=> situation_embedding
    LIMIT match_count;
$$;
