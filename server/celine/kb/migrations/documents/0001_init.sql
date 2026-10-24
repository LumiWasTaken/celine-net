
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE items (
    id               INTEGER PRIMARY KEY,
    path             TEXT NOT NULL UNIQUE,     -- relative to the folder, posix
    sha256           TEXT NOT NULL,
    size             INTEGER NOT NULL,
    mtime            INTEGER NOT NULL,         -- ms
    kind             TEXT NOT NULL,            -- note|text|code|data|html|pdf|docx|subtitles|epub|image|file
    mode             TEXT NOT NULL DEFAULT 'chunked',   -- chunked|full_file
    mime             TEXT,
    title            TEXT NOT NULL,            -- from the file (H1, metadata) or its name
    user_title       TEXT,                     -- the user's; wins when set
    user_description TEXT,                     -- the user's
    ai_description   TEXT,                     -- high level, no spoilers
    ai_model         TEXT,
    ai_generated_at  INTEGER,
    ai_source_hash   TEXT,                     -- sha256 of the file it was written from
    ai_state         TEXT NOT NULL DEFAULT 'pending',   -- pending|done|failed|skipped
    ai_error         TEXT,
    ai_attempts      INTEGER NOT NULL DEFAULT 0,
    state            TEXT NOT NULL,            -- pending|ready|failed
    error            TEXT,
    extractor        TEXT,
    read_hash        TEXT,                     -- sha256 the segments were read from
    pages            INTEGER,
    n_segments       INTEGER NOT NULL DEFAULT 0,
    n_chars          INTEGER NOT NULL DEFAULT 0,
    n_words          INTEGER NOT NULL DEFAULT 0,
    meta             TEXT,
    added_at         INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    author           TEXT NOT NULL DEFAULT 'user',   -- user|celine: who wrote the file
    trashed_at       INTEGER                   -- ms; in the trash: out of listings and search
);
CREATE INDEX items_sha ON items (sha256);
CREATE INDEX items_state ON items (state);

CREATE TABLE segments (
    id       INTEGER PRIMARY KEY,
    item_id  INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    kind     TEXT NOT NULL,                    -- heading|para|cue|cell|caption
    page     INTEGER,
    heading  TEXT,
    t_ms     INTEGER,
    speaker  TEXT,
    text     TEXT NOT NULL,
    hash     TEXT NOT NULL
);
CREATE INDEX segments_item ON segments (item_id, seq);

CREATE TABLE trashed_folders (
    path       TEXT PRIMARY KEY,               -- relative to the folder, posix
    trashed_at INTEGER NOT NULL
) WITHOUT ROWID;

-- [[...]] links in Markdown items: an item name ("other file", lowercase, no .md) or a
-- message reference ("whatsapp:48213"), for backlinks
CREATE TABLE links (
    src    INTEGER NOT NULL,                   -- items.id
    target TEXT NOT NULL,
    PRIMARY KEY (src, target)
) WITHOUT ROWID;
CREATE INDEX links_target ON links (target);

-- map step results of a long file's description, so a stopped job resumes mid-file
CREATE TABLE describe_parts (
    item_id  INTEGER NOT NULL,
    source   TEXT NOT NULL,                    -- sha256 of the file
    part     INTEGER NOT NULL,
    text     TEXT NOT NULL,
    PRIMARY KEY (item_id, source, part)
) WITHOUT ROWID;

CREATE VIRTUAL TABLE search_fts USING fts5(
    body, content='', contentless_delete=1, prefix='2 3',
    tokenize = "unicode61 remove_diacritics 0"
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
