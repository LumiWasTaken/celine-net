-- A chat knowledge base. One file per knowledge base.
-- Applied once per file (its user_version), like every numbered file in this folder.

CREATE TABLE kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Anyone who ever sent a message, and the other side of every 1:1 chat.
-- `key` is the source identity: a phone JID for WhatsApp (LIDs are resolved to the
-- phone JID when the mapping is known), a user id for Discord, `name:<text>` for a
-- text export that only has names.
CREATE TABLE people (
    id           INTEGER PRIMARY KEY,
    key          TEXT NOT NULL UNIQUE,
    phone        TEXT,             -- E.164 with +, when known
    custom_name  TEXT,             -- yours, never touched by an import
    contact_name TEXT,             -- address book (wa.db, a .vcf, a linked device)
    push_name    TEXT,             -- the name they gave themselves (WhatsApp)
    display_name TEXT,             -- Discord global name
    username     TEXT,             -- Discord username
    is_me        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE chats (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'dm',     -- dm | group | channel
    name            TEXT,                           -- from the source: subject, channel name
    custom_name     TEXT,                           -- yours
    parent          TEXT,                           -- Discord server name
    person_id       INTEGER REFERENCES people(id),  -- the other side of a dm
    trashed         INTEGER NOT NULL DEFAULT 0,     -- in the trash: out of lists and search
    message_count   INTEGER NOT NULL DEFAULT 0,
    first_ts        INTEGER,
    last_ts         INTEGER,
    last_message_id INTEGER
);

CREATE TABLE messages (
    id        INTEGER PRIMARY KEY,
    chat_id   INTEGER NOT NULL REFERENCES chats(id),
    key       TEXT NOT NULL,                        -- the source's message id
    sender_id INTEGER REFERENCES people(id),        -- null for your own and for notices
    from_me   INTEGER NOT NULL DEFAULT 0,
    ts        INTEGER NOT NULL,                     -- ms since epoch, UTC
    text      TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT 'text',         -- text | media | system
    media     TEXT,                                 -- json {"type", "caption", "name"}
    reply_key TEXT,                                 -- key of the parent, same chat
    edited    INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER,                             -- deleted on the source (text kept)
    UNIQUE (chat_id, key)
);
CREATE INDEX messages_chat_ts     ON messages (chat_id, ts, id);
CREATE INDEX messages_chat_sender ON messages (chat_id, sender_id);
CREATE INDEX messages_sender_ts   ON messages (sender_id, ts);
CREATE INDEX messages_ts          ON messages (ts);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    text, content='messages', content_rowid='id',
    tokenize = "unicode61 remove_diacritics 2"
);
CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages
WHEN new.text <> '' BEGIN
    INSERT INTO messages_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages
WHEN old.text <> '' BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER messages_fts_au AFTER UPDATE OF text ON messages
WHEN old.text IS NOT new.text BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text)
        SELECT 'delete', old.id, old.text WHERE old.text <> '';
    INSERT INTO messages_fts (rowid, text)
        SELECT new.id, new.text WHERE new.text <> '';
END;

-- Chats deleted and ignored ("Delete and ignore"): no feed record and no import writes
-- them again until they are included again. `kind` and `name` as they were, for the list.
CREATE TABLE ignored_chats (
    key  TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT,
    at   INTEGER NOT NULL
);

-- Edits: the text a message had before each edit (from the connectors' feed).
CREATE TABLE message_edits (
    id         INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    ts         INTEGER NOT NULL,          -- when the edit was seen, ms
    old_text   TEXT
);
CREATE INDEX message_edits_msg ON message_edits (message_id);

-- ---- the semantic index (kb/passages.py; passages_schema.sql has the passages) ----

-- A stretch of one chat between long pauses (kb/sessions.py).
CREATE TABLE sessions (
    id       INTEGER PRIMARY KEY,
    chat_id  INTEGER NOT NULL,
    first_id INTEGER NOT NULL,
    last_id  INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL,
    n_msgs   INTEGER NOT NULL,
    n_chars  INTEGER NOT NULL,
    sig      TEXT NOT NULL             -- hash of the member ids: same members, same session
);
CREATE INDEX sessions_chat ON sessions (chat_id, start_ts);

-- Every non-system message is in exactly one session once the index is built. A
-- message that is in none is new, and its chat is re-derived on the next build.
CREATE TABLE session_members (
    message_id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL
);
CREATE INDEX session_members_session ON session_members (session_id);

-- Messages whose text changed or that were deleted since the last build: their
-- sessions are re-rendered even though their members stayed the same.
CREATE TABLE passage_dirty (
    message_id INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL
);
CREATE TRIGGER passage_dirty_au AFTER UPDATE OF text, media ON messages
WHEN old.text IS NOT new.text OR old.media IS NOT new.media BEGIN
    INSERT OR IGNORE INTO passage_dirty (message_id, chat_id) VALUES (new.id, new.chat_id);
END;
CREATE TRIGGER passage_dirty_ad AFTER DELETE ON messages BEGIN
    INSERT OR IGNORE INTO passage_dirty (message_id, chat_id) VALUES (old.id, old.chat_id);
END;

-- ---- media files and image captions (kb/media.py, kb/media_wa.py) ----
-- Image captions, one per file (content hash). kb/media.py writes, kb/media_text.py reads.
CREATE TABLE media_captions (
    sha256       TEXT PRIMARY KEY,
    state        TEXT NOT NULL,            -- done | failed | skipped
    caption      TEXT,                     -- English, one or two sentences
    visible_text TEXT,                     -- text readable in the image, verbatim
    model        TEXT,                     -- which model wrote it
    endpoint     TEXT,                     -- the Settings model that answered
    prompt_ver   INTEGER NOT NULL DEFAULT 1,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    ms           INTEGER,                  -- how long the model took
    created_at   INTEGER NOT NULL
);
-- What WhatsApp says about each media message's file: hashes and names for the folder
-- matcher, CDN references for a later download through wa-link. From a decrypted
-- msgstore.db (source 'msgstore') and wa-link's raw history/live files ('link').
CREATE TABLE wa_media_meta (
    key           TEXT NOT NULL,           -- messages.key (WhatsApp message id)
    source        TEXT NOT NULL,           -- msgstore | link
    mtype         TEXT,                    -- photo | video | ...
    file_hash     TEXT,                    -- sha256 of the plain file, hex
    file_name     TEXT,
    file_path     TEXT,                    -- on the phone, msgstore only
    file_size     INTEGER,
    mime          TEXT,
    direct_path   TEXT,
    media_key     TEXT,                    -- base64
    enc_file_hash TEXT,                    -- base64
    media_key_ts  INTEGER,
    ts            INTEGER,                 -- ms
    width         INTEGER,                 -- pixels, images and videos
    height        INTEGER,
    PRIMARY KEY (key, source)
);
CREATE INDEX wa_media_meta_hash ON wa_media_meta (file_hash);
CREATE INDEX wa_media_meta_name ON wa_media_meta (file_name);
-- Per message: the last try to fetch its file (Discord refresh, wa-link download), so a
-- message that cannot be fetched is not asked for again on every run.
CREATE TABLE media_fetch (
    message_id INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,
    at         INTEGER NOT NULL,
    state      TEXT NOT NULL,              -- ok | gone | failed
    error      TEXT
);

-- ---- the connector feed (kb/feed.py): edits and deletes waiting for their message ----
CREATE TABLE feed_pending (
    chat_key    TEXT NOT NULL,
    message_key TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'edit' | 'delete'
    ts          INTEGER NOT NULL,
    text        TEXT,
    PRIMARY KEY (chat_key, message_key, kind, ts)
);


-- The semantic index: passages -> vectors, in a chat archive and in Documents. The
-- chat archive's sessions are in chat_schema.sql. See kb/passages.py.
-- Idempotent; additive only. Everything here is derived and can be rebuilt.

-- Overlapping windows inside a session, with a contextual header. `header` and `text`
-- are rendered with the names as they were at build time; the ids are in
-- passage_people and passage_members, and a rename re-renders the passages it touches.
CREATE TABLE passages (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    seq        INTEGER NOT NULL,       -- position inside the session
    first_id   INTEGER NOT NULL,
    last_id    INTEGER NOT NULL,
    start_ts   INTEGER NOT NULL,
    end_ts     INTEGER NOT NULL,
    n_msgs     INTEGER NOT NULL,
    header     TEXT NOT NULL,
    text       TEXT NOT NULL,
    hash       TEXT NOT NULL,          -- of header, text and member ids: same hash, same vector
    embedded   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX passages_session ON passages (session_id, seq);
CREATE INDEX passages_chat_ts ON passages (chat_id, start_ts);
CREATE INDEX passages_ts      ON passages (start_ts);
CREATE INDEX passages_pending ON passages (embedded, start_ts) WHERE embedded = 0;

CREATE TABLE passage_members (
    message_id INTEGER NOT NULL,
    passage_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, passage_id)
) WITHOUT ROWID;
CREATE INDEX passage_members_passage ON passage_members (passage_id);

-- Who wrote in a passage. person_id -1 is you.
CREATE TABLE passage_people (
    passage_id INTEGER NOT NULL,
    person_id  INTEGER NOT NULL,
    n          INTEGER NOT NULL,
    PRIMARY KEY (passage_id, person_id)
) WITHOUT ROWID;
CREATE INDEX passage_people_person ON passage_people (person_id);

-- The vector of a passage: the first `dims` dimensions, L2-normalised, as int8 with a
-- per-vector scale (`v`), and as one sign bit per dimension (`passage_bits.b`, a small
-- table scanned first with Hamming distance). Plain tables scored with sqlite-vec's
-- distance functions: measured faster than vec0 here (see kb/passages.py). The width
-- and model are in kv `passages_vec`.
CREATE TABLE passage_vecs (
    passage_id INTEGER PRIMARY KEY,
    v          BLOB NOT NULL
);
CREATE TABLE passage_bits (
    passage_id INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    start_ts   INTEGER NOT NULL,
    b          BLOB NOT NULL
);
CREATE INDEX passage_bits_chat ON passage_bits (chat_id, start_ts);
CREATE INDEX passage_bits_ts   ON passage_bits (start_ts);

-- Contextual BM25: the header and the text of every passage.
CREATE VIRTUAL TABLE passages_fts USING fts5(
    header, text, content='passages', content_rowid='id',
    tokenize = "unicode61 remove_diacritics 2"
);
CREATE TRIGGER passages_fts_ai AFTER INSERT ON passages BEGIN
    INSERT INTO passages_fts (rowid, header, text) VALUES (new.id, new.header, new.text);
END;
CREATE TRIGGER passages_fts_ad AFTER DELETE ON passages BEGIN
    INSERT INTO passages_fts (passages_fts, rowid, header, text)
        VALUES ('delete', old.id, old.header, old.text);
END;
CREATE TRIGGER passages_fts_au AFTER UPDATE OF header, text ON passages BEGIN
    INSERT INTO passages_fts (passages_fts, rowid, header, text)
        VALUES ('delete', old.id, old.header, old.text);
    INSERT INTO passages_fts (rowid, header, text) VALUES (new.id, new.header, new.text);
END;
