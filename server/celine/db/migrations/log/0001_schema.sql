-- The whole schema of var/log.db, attached to celine.db as `log` (core/store.py): the
-- append-only tables, pruned by count. A fresh file applies it and is at 1. No foreign
-- keys: a file cannot reference another; conversations/store.py deletes what belongs to
-- a purged conversation or message, terminals/store.py to a purged terminal.

-- each conversation's event log (core/store.py); seq is gapless per conversation
CREATE TABLE events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  seq             INTEGER NOT NULL,
  type            TEXT NOT NULL,
  data            TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE (conversation_id, seq)
);
CREATE INDEX events_tail ON events(conversation_id, seq);

-- the global feed every page follows
CREATE TABLE global_events (
  id         INTEGER PRIMARY KEY,
  type       TEXT NOT NULL,
  data       TEXT NOT NULL,            -- JSON, the frame without id and type
  created_at TEXT NOT NULL
);

-- what the server answered for a reply, as it came
CREATE TABLE raw_responses (
  message_id TEXT PRIMARY KEY,
  body       TEXT NOT NULL
);

-- every model request a reply's steps sent (turns/store.py)
CREATE TABLE requests (
  message_id        TEXT NOT NULL,            -- the reply this step streamed into
  step              INTEGER NOT NULL,         -- the turn's iteration
  turn_id           TEXT NOT NULL,
  provider          TEXT NOT NULL,            -- backend id, "llama-swap"
  model             TEXT NOT NULL,            -- the model that answered
  body              BLOB NOT NULL,            -- zlib(json)
  estimate          INTEGER,                  -- our token estimate of it
  prompt_tokens     INTEGER,                  -- the server's count
  completion_tokens INTEGER,
  window            INTEGER,                  -- n_ctx believed at the time
  finish_reason     TEXT,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (message_id, step)
);
CREATE INDEX requests_turn ON requests(turn_id);

-- what the user typed or pressed on her terminals (terminals/store.py)
CREATE TABLE terminal_commands (
  id          TEXT PRIMARY KEY,
  terminal_id TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('line','control')),
  text        TEXT NOT NULL,
  result      TEXT,
  note        TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX terminal_commands_at ON terminal_commands(created_at DESC);
