-- A drone (sub_agents/drone.py): a sub-agent whose CLI runs on a device. The device
-- (NULL: its shellbox), its opencode session there, the rowid of the last message sent
-- to it, the id of the last answer of the session already counted, and the step it is on.
ALTER TABLE conversations ADD COLUMN agent_device TEXT;
ALTER TABLE conversations ADD COLUMN agent_session TEXT;
ALTER TABLE conversations ADD COLUMN agent_sent INTEGER;
ALTER TABLE conversations ADD COLUMN agent_seen TEXT;
ALTER TABLE conversations ADD COLUMN agent_step TEXT;
