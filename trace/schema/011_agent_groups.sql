-- 011: agent group chat (color-coded "add your agents to a group chat" feature). See
-- memory/agent-groupchat-feature.md. A group is a named set of agent members owned by a
-- user; each agent carries a color for its message bubbles (auto-assigned, user-overridable).

ALTER TABLE agents ADD COLUMN IF NOT EXISTS color TEXT;   -- hex; NULL = derive default from id

CREATE TABLE IF NOT EXISTS chat_groups (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id   UUID NOT NULL REFERENCES profiles(id),
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_groups_owner_idx ON chat_groups (owner_id);

CREATE TABLE IF NOT EXISTS chat_group_members (
    group_id UUID NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, agent_id)
);

ALTER TABLE chat_groups        ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_group_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own chat_groups" ON chat_groups
    FOR ALL USING (owner_id = auth.uid());
CREATE POLICY "Users manage own chat_group_members" ON chat_group_members
    FOR ALL USING (group_id IN (SELECT id FROM chat_groups WHERE owner_id = auth.uid()));
