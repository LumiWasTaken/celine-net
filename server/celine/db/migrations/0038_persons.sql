-- A person record holds what is known about a person beyond their accounts: how they
-- relate to the user, and notes, each with who wrote it (the user or the agent). The
-- About text becomes the person's first note. A changed or removed note stays as a row.
ALTER TABLE identities ADD COLUMN relation TEXT NOT NULL DEFAULT '';

CREATE TABLE person_notes (
  id          INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
  text        TEXT NOT NULL,
  author      TEXT NOT NULL CHECK (author IN ('user','agent')),
  created_at  TEXT NOT NULL,
  replaced_at TEXT            -- when it was changed or removed; NULL: it stands
);
CREATE INDEX person_notes_identity ON person_notes(identity_id, replaced_at);

INSERT INTO person_notes (identity_id, text, author, created_at)
  SELECT id, about, 'user', updated_at FROM identities WHERE TRIM(about) <> '';

ALTER TABLE identities DROP COLUMN about;
