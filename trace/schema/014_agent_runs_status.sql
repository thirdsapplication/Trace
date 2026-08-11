-- 014: agent_runs.status CHECK constraint never matched the app's vocabulary.
--
-- Every "Run now" died on
--     23514 new row for relation "agent_runs" violates check constraint
--           "agent_runs_status_check"   (status = 'ran')
-- thrown from registry.run_add inside _record_deployment_run (app.py:1298), which sits
-- OUTSIDE run_deployment_now's try/except — so the background thread died, no run row was
-- written, no DM was sent, and the UI showed nothing at all. Same family as 012/013:
-- agent_runs predates this repo's migrations, so its constraint was written against an
-- older status vocabulary.
--
-- The statuses the code actually writes:
--   ran            — deployment run accepted by the platform (app.py run_deployment_now)
--   completed      — run finished, actions summarized (runner._build_run_record)
--   executed       — a staged action performed on approval (runner.execute_pending)
--   failed         — run could not start, or errored
--   needs_input    — agent paused on a blocking question (Gap #3 uncertainty loop)
--   needs_approval — run produced staged proposals awaiting the human (Spec 8)

ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_status_check;

ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check
    CHECK (status IN ('ran', 'completed', 'executed', 'failed',
                      'needs_input', 'needs_approval'));
