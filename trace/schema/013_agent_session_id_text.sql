-- 013: agents.session_id holds a TRACE SESSION ID, not a UUID.
--
-- Follow-on to 012, same class of bug. Trace session ids are timestamp strings like
-- `20260811-165721` (sessions.create_session), but agents.session_id was declared UUID,
-- so the registry upsert failed with
--     22P02 invalid input syntax for type uuid: "20260811-165721"
-- immediately after 012 unblocked agents.id.
--
-- Note the other session-id columns are already TEXT and need no change:
-- agent_runs.run_session_id (003), pending_actions.run_session_id (003),
-- lesson_applications.run_session_id (008), session_summaries.session_id (003).
--
-- Uses the same policy save/restore harness as 012, since a policy expression
-- referencing the column would otherwise block the retype.

BEGIN;

CREATE TEMP TABLE _sess_pols ON COMMIT DROP AS
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
WHERE pol.polrelid = 'public.agents'::regclass;

CREATE TEMP TABLE _sess_fks ON COMMIT DROP AS
SELECT con.conname::text            AS name,
       con.conrelid::regclass::text AS tbl,
       pg_get_constraintdef(con.oid) AS def
FROM pg_constraint con
JOIN pg_attribute att
  ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
WHERE con.contype = 'f'
  AND con.conrelid = 'public.agents'::regclass
  AND att.attname = 'session_id';

DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM _sess_pols LOOP
        EXECUTE format('DROP POLICY %I ON %s', r.name, r.tbl);
    END LOOP;
    FOR r IN SELECT * FROM _sess_fks LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.name);
    END LOOP;
END $$;

ALTER TABLE agents ALTER COLUMN session_id TYPE text USING session_id::text;

-- The FK is dropped PERMANENTLY, not restored: agents_session_id_fkey pointed at
-- sessions(id), which is a DB-generated UUID, while this column holds the LOCAL sid
-- (`20260811-165721`). It has to hold the local sid because that value is used to read
-- artifacts off disk — _load_connection(agent["session_id"]) at app.py:544 and again at
-- app.py:2222 resolve captures/sessions/<sid>/. A text column cannot reference a uuid
-- primary key, so the constraint cannot survive. sessions.local_sid is the join key if
-- integrity is ever wanted back (see sessions.supabase_session_id, sessions.py:89).
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM _sess_pols LOOP
        EXECUTE format('CREATE POLICY %I ON %s AS %s FOR %s TO %s%s%s',
                       r.name, r.tbl, r.permissive, r.cmd, r.roles,
                       CASE WHEN r.qual IS NOT NULL
                            THEN ' USING (' || r.qual || ')' ELSE '' END,
                       CASE WHEN r.with_check IS NOT NULL
                            THEN ' WITH CHECK (' || r.with_check || ')' ELSE '' END);
    END LOOP;
END $$;

COMMIT;
