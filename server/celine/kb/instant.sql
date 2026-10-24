-- Instant search: a normalised shadow of `messages` (see kb/instant.py, kb/normalize.py).
-- Runs on every open. Additive only, IF NOT EXISTS; nothing here touches `messages`
-- beyond the triggers that note which rows changed.

-- One row per indexed message. `sk` is a time-ordered key (ts * 1024 + low id bits,
-- bumped on collision) and the rowid of search_fts, so "newest first" is a backwards
-- walk of the index that stops after one page.
CREATE TABLE IF NOT EXISTS search_map (
    sk      INTEGER PRIMARY KEY,
    id      INTEGER NOT NULL UNIQUE,     -- messages.id
    chat_id INTEGER NOT NULL,
    person  INTEGER,                     -- sender_id, -1 for your own, null for notices
    ym      INTEGER NOT NULL             -- local year*100+month, for the histogram
);

-- body: normalised text + extra spellings; f: filter tokens (all c<chat> p<person> me
-- k<kind> m<media type> link reply del edit).
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    body, f, content = '', contentless_delete = 1,
    tokenize = "unicode61 remove_diacritics 2", prefix = '1 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_vocab USING fts5vocab(search_fts, 'col');

-- Rows to (re)index. Filled by triggers, drained by kb/instant.py. Any writer works,
-- no SQL function has to be registered on its connection.
CREATE TABLE IF NOT EXISTS search_dirty (id INTEGER PRIMARY KEY);

-- Not "INSERT OR IGNORE": inside a trigger, SQLite replaces OR IGNORE with the outer
-- statement's conflict policy, so an upsert (INSERT ... ON CONFLICT DO UPDATE) that
-- updated a message still waiting here failed with a UNIQUE error.
CREATE TRIGGER IF NOT EXISTS search_dirty_ai AFTER INSERT ON messages BEGIN
    INSERT INTO search_dirty (id) SELECT new.id
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE id = new.id);
END;
CREATE TRIGGER IF NOT EXISTS search_dirty_au AFTER UPDATE ON messages BEGIN
    INSERT INTO search_dirty (id) SELECT new.id
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE id = new.id);
END;
CREATE TRIGGER IF NOT EXISTS search_dirty_ad AFTER DELETE ON messages BEGIN
    INSERT INTO search_dirty (id) SELECT old.id
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE id = old.id);
END;
