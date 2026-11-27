-- A person is more than two names: the profile a source shows (About or bio, pronouns),
-- whether the account still exists, and every name they had, with when it was theirs.
ALTER TABLE people ADD COLUMN bio TEXT;                          -- Discord bio, WhatsApp About
ALTER TABLE people ADD COLUMN pronouns TEXT;
ALTER TABLE people ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0;  -- the account is gone; its last names stay
ALTER TABLE people ADD COLUMN profile_ts INTEGER;                -- when the source last gave the profile
ALTER TABLE people ADD COLUMN seen_ts INTEGER;                   -- when the source last named them

-- One row per value a person had: kind is username, display_name, push_name,
-- contact_name, nick (scope: the server it is their nickname on), bio or pronouns.
-- from_ts NULL: theirs since before it was recorded. to_ts NULL: still theirs.
CREATE TABLE person_names (
    id        INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    scope     TEXT NOT NULL DEFAULT '',
    value     TEXT NOT NULL,
    from_ts   INTEGER,
    to_ts     INTEGER
);
CREATE INDEX person_names_person ON person_names(person_id, kind, scope, to_ts);

INSERT INTO person_names (person_id, kind, value)
  SELECT id, 'username', TRIM(username) FROM people WHERE TRIM(COALESCE(username, '')) <> ''
  UNION ALL
  SELECT id, 'display_name', TRIM(display_name) FROM people WHERE TRIM(COALESCE(display_name, '')) <> ''
  UNION ALL
  SELECT id, 'push_name', TRIM(push_name) FROM people WHERE TRIM(COALESCE(push_name, '')) <> ''
  UNION ALL
  SELECT id, 'contact_name', TRIM(contact_name) FROM people WHERE TRIM(COALESCE(contact_name, '')) <> '';

-- A direct chat whose other side was never recorded, with exactly one person besides you
-- writing in it: that person is its other side. A direct chat with its other side
-- stores no name of its own (the person's name is its name).
UPDATE chats SET
    person_id = (SELECT m.sender_id FROM messages m
                 WHERE m.chat_id = chats.id AND m.from_me = 0 AND m.sender_id IS NOT NULL
                 LIMIT 1),
    name = NULL
WHERE kind = 'dm' AND person_id IS NULL
  AND (SELECT COUNT(DISTINCT m.sender_id) FROM messages m
       WHERE m.chat_id = chats.id AND m.from_me = 0 AND m.sender_id IS NOT NULL) = 1;
