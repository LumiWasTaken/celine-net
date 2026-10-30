-- A sub-agent started from one of her agent templates (sub_agents/templates.py): the
-- template's name; its prompt and tools are read from the note at every turn.
ALTER TABLE conversations ADD COLUMN agent_template TEXT;
