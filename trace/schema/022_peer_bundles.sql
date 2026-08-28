-- 022: peer bundles — the scrubbed material one agent publishes so OTHER agents can
-- learn from it.
--
-- A learning cycle that reads only its own agent's history can only relearn what that
-- agent already lived through: the same wasted re-reads, found the same slow way, once
-- per customer. A bundle is the carrier that lets one agent's efficiency lesson reach
-- the next one — trace/agent/peers.py decides who is a peer, trace/agent/learn.py feeds
-- the selected bundles into the next analysis.
--
-- THE DECISION THIS TABLE ENCODES: a bundle is published by the agent that OWNS the
-- material, at the end of its own learning cycle, and is read later by other agents.
-- The obvious alternative — keep raw sessions and scrub them per reader, once a reader
-- turns out to want them — loses twice. It costs a scrub call per (reader, writer)
-- pair, so model spend grows with the square of the fleet; publishing once per agent
-- per cycle is ONE extra call no matter how many agents later read the row. And it
-- would put raw transcripts inside a cycle that does not own them. Here raw session
-- material never leaves the cycle that produced it: the only thing that ever crosses an
-- agent boundary is what the scrub + verify pass (trace/agent/scrub.py) already cleared.
--
-- verified is the read gate, same posture as agent_decisions.global_verified (010), and
-- DEFAULT FALSE makes it fail-closed: a row written by any path that does not explicitly
-- assert a passed verify is invisible to selection rather than shared by accident.
--
-- memory holds the scrubbed memory documents but never /shared_lessons.md. That document
-- is itself peer material this agent received; republishing it would send other agents'
-- lessons back out under this agent's name, and corroboration counts would then be
-- counting echoes instead of independent observations.
--
-- owner_id is written but nothing in selection reads it yet. It is here so per-account
-- consent can land later as a WHERE clause on an existing column rather than as a
-- migration against a table that by then has rows in it.
--
-- agent_id is TEXT (platform string), not UUID — see 012.

CREATE TABLE IF NOT EXISTS peer_bundles (
    id          BIGSERIAL PRIMARY KEY,                 -- publication order; nothing addresses a bundle by id
    agent_id    TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    owner_id    UUID REFERENCES profiles(id),
    sessions    JSONB NOT NULL DEFAULT '[]'::jsonb,    -- scrubbed session records
    memory      JSONB NOT NULL DEFAULT '{}'::jsonb,    -- scrubbed docs; never /shared_lessons.md
    fingerprint JSONB NOT NULL DEFAULT '{}'::jsonb,    -- {tools:{name:count}, sessions, avg_tokens}
    embedding   vector(1024),                          -- Voyage voyage-3.5, as agent_embeddings (008)
    verified    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- peer_bundles_latest wants the newest verified bundle PER AGENT, so the scan is
-- newest-first within an agent and stops early; a bundle's predecessors stay for audit.
CREATE INDEX IF NOT EXISTS peer_bundles_agent_idx
    ON peer_bundles (agent_id, created_at DESC);

-- Same shape as agent_embeddings_ivf (008): the same-job tier compares these vectors by
-- cosine, and both sides come from voyage-3.5 at 1024 dims.
CREATE INDEX IF NOT EXISTS peer_bundles_ivf
    ON peer_bundles USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

ALTER TABLE peer_bundles ENABLE ROW LEVEL SECURITY;
-- Written and read via the service role (supa.get_admin()), like learning_cycles (021)
-- and agent_embeddings (008). Cross-agent reads are the entire point of this table, so
-- an owner-scoped user policy would be the wrong shape here; selection filters in SQL.
-- CREATE POLICY takes no IF NOT EXISTS, so re-applying this file fails with
--     42710  policy "Service role manages peer_bundles" for table "peer_bundles" already exists
-- and that error is SAFE to see — the table and both indexes above are idempotent, so
-- nothing is left half-applied. 021 has the identical hazard and an operator read the
-- same 42710 as a failed migration, which cost a support round.
CREATE POLICY "Service role manages peer_bundles" ON peer_bundles
    FOR ALL USING (auth.role() = 'service_role');
