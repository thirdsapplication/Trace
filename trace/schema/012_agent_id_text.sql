-- 012: agents.id is a PLATFORM STRING, not a UUID.
--
-- Managed Agents issues ids like `agent_0181qZaoYACw6oSLzpmsSYj2`. The registry has
-- always written that string (registry._agent_to_sb: sb["id"] = agent["agent_id"]),
-- but `agents.id` and every referencing agent_id column were declared UUID. So EVERY
-- deploy in Supabase mode failed at the registry write with
--     22P02 invalid input syntax for type uuid: "agent_0181qZ..."
-- AFTER the platform-side agent was already created and live — the exception was
-- swallowed into the deploy job's per-bundle errors dict (app.py:1189), so the UI
-- reported a failed go-live while leaving an orphaned live agent behind.
--
-- File-mode has no type constraint on the key, which is why the whole test suite
-- passes and never caught this.
--
-- Retypes agents.id and every agent_id FK column to TEXT, preserving each
-- constraint's original definition (including ON DELETE CASCADE), and recreates
-- match_lessons, whose RETURNS TABLE pins agent_id to uuid.

BEGIN;

-- 1. Remember every FK pointing at agents(id), verbatim. Keyed by relation OID —
--    never by parsed name, since regclass drops the schema when it's in search_path.
CREATE TEMP TABLE _agent_fks ON COMMIT DROP AS
SELECT con.conname::text            AS name,
       con.conrelid                 AS reloid,
       con.conrelid::regclass::text AS tbl,
       att.attname::text            AS col,
       pg_get_constraintdef(con.oid) AS def
FROM pg_constraint con
JOIN pg_attribute att
  ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
WHERE con.contype = 'f' AND con.confrelid = 'public.agents'::regclass;

-- 2. Remember every RLS policy on those tables (plus agents itself). Postgres refuses
--    to retype a column any policy expression depends on, so they come off and go back
--    on too. Read from pg_policy so roles/cmd/permissive round-trip exactly.
CREATE TEMP TABLE _agent_pols ON COMMIT DROP AS
SELECT pol.polname::text            AS name,
       pol.polrelid::regclass::text AS tbl,
       CASE WHEN pol.polpermissive THEN 'PERMISSIVE' ELSE 'RESTRICTIVE' END AS permissive,
       CASE pol.polcmd WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT'
                       WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE'
                       ELSE 'ALL' END AS cmd,
       COALESCE((SELECT string_agg(quote_ident(rolname), ', ')
                 FROM pg_roles WHERE oid = ANY (pol.polroles)), 'public') AS roles,
       pg_get_expr(pol.polqual, pol.polrelid)      AS qual,
       pg_get_expr(pol.polwithcheck, pol.polrelid) AS with_check
FROM pg_policy pol
WHERE pol.polrelid IN (SELECT reloid FROM _agent_fks
                       UNION SELECT 'public.agents'::regclass::oid);

-- 3. Drop the policies, then the FKs, so the column types can change.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM _agent_pols LOOP
        EXECUTE format('DROP POLICY %I ON %s', r.name, r.tbl);
    END LOOP;
    FOR r IN SELECT * FROM _agent_fks LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.name);
    END LOOP;
END $$;

-- 4. Retype every referencing column, then the primary key itself.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT DISTINCT tbl, col FROM _agent_fks LOOP
        EXECUTE format('ALTER TABLE %s ALTER COLUMN %I TYPE text USING %I::text',
                       r.tbl, r.col, r.col);
    END LOOP;
END $$;

ALTER TABLE agents ALTER COLUMN id DROP DEFAULT;   -- gen_random_uuid() no longer applies
ALTER TABLE agents ALTER COLUMN id TYPE text USING id::text;

-- 5. Put the FKs back exactly as they were, then the policies.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM _agent_fks LOOP
        EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s', r.tbl, r.name, r.def);
    END LOOP;
    FOR r IN SELECT * FROM _agent_pols LOOP
        EXECUTE format('CREATE POLICY %I ON %s AS %s FOR %s TO %s%s%s',
                       r.name, r.tbl, r.permissive, r.cmd, r.roles,
                       CASE WHEN r.qual IS NOT NULL
                            THEN ' USING (' || r.qual || ')' ELSE '' END,
                       CASE WHEN r.with_check IS NOT NULL
                            THEN ' WITH CHECK (' || r.with_check || ')' ELSE '' END);
    END LOOP;
END $$;

COMMIT;

-- 6. match_lessons pins agent_id to uuid in RETURNS TABLE; a return-type change needs
--    DROP, not CREATE OR REPLACE. Body is unchanged from 009 apart from that column.
DROP FUNCTION IF EXISTS match_lessons(vector, uuid, vector, boolean, float, float, float, int);

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
