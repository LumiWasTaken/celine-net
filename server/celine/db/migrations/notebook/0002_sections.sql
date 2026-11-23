-- The semantic index of the notebook (skills/notebook_semantic.py), beside the word
-- index (notebook_fts): the live notes cut into canonical sections and chunks
-- (notebook_index.cut), each chunk with the hash of its canonical embedding payload
-- (path + summary + heading + chunk text, kb.toml [embeddings] width and quantisation)
-- and, once the background scheduler embedded it, one int8 vector with its sign bits
-- (kb/embed.quantize). notebook_semantic_state remembers which note content the
-- sections were cut from, so a note edited to the same length is still seen as changed.
-- notebook_meta holds the identity of the model that made the vectors (kb/embed
-- fingerprint probes), its actual width and the payload render version: an index made
-- by another model, or with another render algorithm, is dropped and built again. The
-- note files stay the source of truth; a vector without its section's hash is stale and
-- is embedded again.

DROP TABLE IF EXISTS notebook_meta;
DROP TABLE IF EXISTS notebook_semantic_state;
DROP TABLE IF EXISTS notebook_sections;
DROP TABLE IF EXISTS notebook_vecs;

-- the hash of the note's text as the index last read it (skills/notebook_index.py
-- reconcile compares it, so an equal-length external edit is still reindexed)
ALTER TABLE notebook_index ADD COLUMN content TEXT NOT NULL DEFAULT '';

CREATE TABLE notebook_semantic_state (
  path    TEXT PRIMARY KEY,
  content TEXT NOT NULL     -- hash of the note text + summary the sections were cut from
);

CREATE TABLE notebook_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE notebook_sections (
  path    TEXT NOT NULL,
  nth     INTEGER NOT NULL,   -- the section's ordinal in the note
  chunk   INTEGER NOT NULL,   -- the chunk's ordinal within its section
  heading TEXT NOT NULL,      -- the `## ` heading, "" for the lines before the first
  chars   INTEGER NOT NULL,   -- the chunk's characters
  hash    TEXT NOT NULL,      -- hash of its canonical embedding payload
  PRIMARY KEY (path, nth, chunk)
);

CREATE TABLE notebook_vecs (
  path  TEXT NOT NULL,
  nth   INTEGER NOT NULL,
  chunk INTEGER NOT NULL,
  hash  TEXT NOT NULL,        -- the payload hash this vector was made from
  int8  BLOB NOT NULL,        -- the quantised vector (kb/embed.quantize)
  bits  BLOB NOT NULL,        -- one sign bit per dimension
  PRIMARY KEY (path, nth, chunk)
);
