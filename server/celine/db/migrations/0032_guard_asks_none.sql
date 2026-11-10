-- The guard asks nobody any more (Require approval is on Settings, Tools since 0031, which
-- closed every open ask): its decisions lose `asked`.
CREATE TABLE guard_decisions_new (
  id              INTEGER PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id         TEXT NOT NULL,
  message_id      TEXT,
  tool_call_id    TEXT NOT NULL,
  tool            TEXT NOT NULL,
  mode            TEXT NOT NULL CHECK (mode IN ('watch','on')),
  layer           INTEGER NOT NULL,
  rule            TEXT NOT NULL,
  does            TEXT,
  decision        TEXT NOT NULL CHECK (decision IN ('waiting','allow','would_stop','stop',
                    'failed','unreachable','found','closed','approved','denied','passed')),
  link            TEXT,
  source          TEXT,
  quote           TEXT,
  why             TEXT,
  class           TEXT,
  sink            TEXT,
  piece           TEXT CHECK (piece IS NULL OR length(piece) <= 300),
  froze           INTEGER NOT NULL DEFAULT 0,
  p               REAL,
  scores          TEXT,
  model           TEXT,
  ms              INTEGER,
  created_at      TEXT NOT NULL
);
INSERT INTO guard_decisions_new SELECT * FROM guard_decisions;
DROP TABLE guard_decisions;
ALTER TABLE guard_decisions_new RENAME TO guard_decisions;
CREATE INDEX guard_decisions_call    ON guard_decisions(tool_call_id);
CREATE INDEX guard_decisions_message ON guard_decisions(message_id);
CREATE INDEX guard_decisions_turn    ON guard_decisions(turn_id);
