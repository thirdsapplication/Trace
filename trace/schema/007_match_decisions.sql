-- 007: decision retrieval RPC (Gap #3 B4). Cosine similarity search over
-- decision_embeddings joined to agent_decisions, returning ONLY the de-identified
-- generalized lesson (never raw question/answer/context) — the single crossing point
-- described in docs/decision_memory_design.md §5.
--
-- Owner-scoped (T0/T1): the caller passes the owner id and only that owner's reusable
-- lessons are searched. Cross-user tiers (T2/T3) will extend this with a scope arg + the
-- DECISION_SHARE_GLOBAL gate in B5. SECURITY DEFINER so it can read the service-role-only
-- decision_embeddings table; it still restricts rows to the passed owner.

CREATE OR REPLACE FUNCTION match_decisions(
    query_embedding vector(1024),
    owner uuid,
    match_count int DEFAULT 3,
    min_similarity float DEFAULT 0.5
)
RETURNS TABLE (decision_id uuid, generalized_lesson text, similarity float)
LANGUAGE sql
STABLE
SECURITY DEFINER
AS $$
    SELECT d.id,
           d.generalized_lesson,
           1 - (e.embedding <=> query_embedding) AS similarity
    FROM decision_embeddings e
    JOIN agent_decisions d ON d.id = e.decision_id
    WHERE d.reusable = true
      AND d.generalized_lesson IS NOT NULL
      AND d.owner_id = owner
      AND 1 - (e.embedding <=> query_embedding) >= min_similarity
    ORDER BY e.embedding <=> query_embedding
    LIMIT match_count;
$$;
