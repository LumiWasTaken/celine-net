-- who may see a conversation, a notebook class (guard/classify.py): NULL until classed,
-- read as sensitive; privacy_owner 1 when the owner set it (never classed over)
ALTER TABLE conversations ADD COLUMN privacy TEXT
  CHECK (privacy IN ('public', 'informal', 'nsfw', 'sensitive'));
ALTER TABLE conversations ADD COLUMN privacy_owner INTEGER NOT NULL DEFAULT 0;
