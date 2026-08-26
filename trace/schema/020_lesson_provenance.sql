-- 020: record WHERE a lesson came from, so applicability can be decided by facts.
--
-- Cross-user sharing currently gates on one cosine between two agent "kind" vectors,
-- each an embedding of "{name}. {description}. Does: {trigger}". That compares how two
-- agents were DESCRIBED, not what they do: identical workflows worded differently score
-- low, unrelated ones in similar corporate prose score high, and the 0.60 threshold was
-- never validated against data because there has never been any.
--
-- Shared apps are a fact rather than a similarity. An agent either touches Shopify or it
-- does not, and a rule learned while working in an app is far likelier to hold for
-- someone else working in that same app than for someone whose description merely reads
-- alike. source_apps makes that checkable.
--
-- source_team_id is here so corroboration can later count DISTINCT ORGANISATIONS rather
-- than distinct users — two people at one company converging on a rule share a handbook,
-- which is one observation, not two.
--
-- Both are nullable and nothing reads them as required: rows written before this
-- migration keep working, and retrieval falls back to today's behaviour whenever either
-- side is unknown.

ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS source_apps TEXT[];
ALTER TABLE agent_decisions ADD COLUMN IF NOT EXISTS source_team_id UUID;

-- Retrieval filters on these after match_lessons returns, so no index is needed for the
-- k<=5 rows a query comes back with. Add one here if that ever moves into SQL.
