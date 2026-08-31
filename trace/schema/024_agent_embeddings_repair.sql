-- 024: repair agent_embeddings, which has never accepted a single row.
--
-- Found 2026-08-31 from a Render log line Philip pasted:
--
--   [guidance] retrieval failed for agent_01BTWP4m7TXdMe...: APIError:
--   {'message': 'there is no unique or exclusion constraint matching the ON CONFLICT
--    specification', 'code': '42P10'}
--
-- Two independent defects in the live table, either of which alone is fatal, and both
-- silent because every caller of the retrieval path is best-effort:
--
--   1. NO UNIQUE CONSTRAINT on agent_id. registry.agent_embedding_upsert issues
--      .upsert(..., on_conflict="agent_id"), which Postgres rejects with 42P10 unless a
--      unique or exclusion constraint covers exactly that column. 008 declared
--      "agent_id UUID PRIMARY KEY"; the live table has only a foreign key, so the
--      primary key was lost somewhere between 008 and 012's conversion of agent ids to
--      TEXT.
--
--   2. embedding IS vector(1536), BUT THE CODE WRITES 1024. Probed directly:
--      "expected 1536 dimensions, not 1024". Voyage voyage-3.5 is requested at
--      output_dimension=1024 and every other embedding column in this schema is
--      vector(1024) — decision_embeddings (006), peer_bundles (022), and the
--      match_lessons RPC parameters (009/012/018). NOTHING in this repo ever declared
--      agent_embeddings at 1536, so the live table did not come from these files.
--
-- Consequence: agent_embeddings has 0 rows and could never have had any. The agent-kind
-- vector is half of the cross-user retrieval gate in match_lessons (a shipping lesson
-- must not reach an invoice agent), so with it permanently empty, lesson_promotions and
-- lesson_applications were also stuck at 0. The whole fleet-learning feature was inert.
--
-- DELIBERATELY SCOPED TO THIS TABLE. decision_embeddings and peer_bundles were probed
-- and also lack a unique constraint on their key, but neither needs one: decision
-- embeddings are written delete-then-insert, and peer_bundles is append-only by design
-- (its history is read back newest-first). Adding a unique constraint to peer_bundles
-- would BREAK it. Both were confirmed to accept vector(1024) correctly.
--
-- Safe to run: the table is empty, so replacing the column loses nothing.
-- Idempotent: re-running is a no-op.

-- The ivfflat index depends on the column and must go first.
DROP INDEX IF EXISTS agent_embeddings_ivf;

ALTER TABLE agent_embeddings DROP COLUMN IF EXISTS embedding;
ALTER TABLE agent_embeddings ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- ADD CONSTRAINT has no IF NOT EXISTS on older Postgres; check the catalogue instead.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'agent_embeddings'::regclass
          AND contype IN ('p', 'u')
          AND conkey = ARRAY[
              (SELECT attnum FROM pg_attribute
               WHERE attrelid = 'agent_embeddings'::regclass AND attname = 'agent_id')
          ]::smallint[]
    ) THEN
        ALTER TABLE agent_embeddings
            ADD CONSTRAINT agent_embeddings_agent_id_key UNIQUE (agent_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS agent_embeddings_ivf
    ON agent_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
