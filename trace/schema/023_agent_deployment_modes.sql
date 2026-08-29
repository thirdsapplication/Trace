-- 023 — whole-agent approval + the staging/execution deployment pair.
--
-- An agent used to have ONE deployment, whose opening message carried either a staging
-- directive or a plain task line. Deployments are immutable, so the per-app `staged_apps`
-- toggle could not reach a platform-fired run: the in-app path read this row while the
-- scheduled run replayed whatever text the deployment was created with.
--
-- Now an agent has up to two deployments against the same platform agent and memory
-- store — staging and execution — and exactly one is unpaused. Approval is a whole-agent
-- setting, because "stage, but only in Gmail" is not a mode a single opening message can
-- express.
--
-- `staged_apps` is left in place, unread by new code, so a row written before this
-- migration can still be interpreted (non-empty => approval required) and so a rollback
-- does not lose the setting.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS approval_required BOOLEAN;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS deployments JSONB DEFAULT '{}'::jsonb;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS deployment_mode TEXT;

-- Backfill: whatever the row said about staged apps is what the human wanted.
UPDATE agents
   SET approval_required = (staged_apps IS NOT NULL
                            AND jsonb_array_length(COALESCE(staged_apps, '[]'::jsonb)) > 0)
 WHERE approval_required IS NULL;

-- Existing agents keep their single deployment as the ACTIVE one; `deployment_id`
-- continues to point at it, so every existing reader keeps working. The second
-- deployment is created on the next deploy or cadence change.
UPDATE agents
   SET deployment_mode = CASE WHEN approval_required THEN 'staging' ELSE 'execution' END
 WHERE deployment_mode IS NULL AND deployment_id IS NOT NULL;

UPDATE agents
   SET deployments = jsonb_build_object(
         CASE WHEN approval_required THEN 'staging' ELSE 'execution' END, deployment_id)
 WHERE deployment_id IS NOT NULL
   AND (deployments IS NULL OR deployments = '{}'::jsonb);
