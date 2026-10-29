-- A tool loads on its own (turns/loads.py): context_loaded takes the kind 'tool'.
CREATE TABLE context_loaded_new (
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  kind            TEXT NOT NULL CHECK (kind IN ('note', 'skill', 'tool')),
  name            TEXT NOT NULL,
  touched         INTEGER NOT NULL,
  PRIMARY KEY (conversation_id, kind, name)
);
INSERT INTO context_loaded_new SELECT conversation_id, kind, name, touched FROM context_loaded;
DROP TABLE context_loaded;
ALTER TABLE context_loaded_new RENAME TO context_loaded;
