-- The whole schema of var/celine.db. A fresh database applies it and is at 1; later
-- files apply on top of it in order.

-- ---- the app -------------------------------------------------------------------------

CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- her one row (her.py)
CREATE TABLE her (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  name       TEXT NOT NULL,
  pronouns   TEXT NOT NULL DEFAULT 'she',
  color      TEXT NOT NULL,
  portrait   TEXT REFERENCES blobs(sha256),     -- her face, a blob
  persona    TEXT NOT NULL,
  talk       TEXT,                              -- how she chats; NULL: the shipped one
  voice      TEXT NOT NULL DEFAULT '{}',        -- {banned, bias, on} (settings/voice.py)
  named      INTEGER NOT NULL DEFAULT 0,        -- her name was saved once (first run)
  updated_at TEXT NOT NULL
);

-- her mood over time (skills/mind.py)
CREATE TABLE moods (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  at         TEXT NOT NULL,
  emotion    TEXT NOT NULL,
  strength   REAL NOT NULL CHECK (strength BETWEEN 0 AND 1),
  why        TEXT NOT NULL DEFAULT '',
  by         TEXT NOT NULL,
  message_id TEXT REFERENCES messages(id) ON DELETE SET NULL
);
CREATE INDEX moods_at ON moods(at);

-- every long text's History (texts.py)
CREATE TABLE text_versions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  of         TEXT NOT NULL,
  text       TEXT NOT NULL,
  by         TEXT NOT NULL,
  change     TEXT NOT NULL,
  size       INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX text_versions_of ON text_versions(of, id);

-- ---- conversations and the message tree ----------------------------------------------

CREATE TABLE conversations (
  id              TEXT PRIMARY KEY,
  title           TEXT NOT NULL DEFAULT '',
  pinned          INTEGER NOT NULL DEFAULT 0,
  trigger_key     TEXT UNIQUE,              -- a trigger's key; NULL if manual
  active_leaf_id  TEXT,                     -- the tree is self-referential: no FK
  trim_below      INTEGER NOT NULL DEFAULT 0,  -- events below this have been pruned
  next_seq        INTEGER NOT NULL DEFAULT 1,  -- the writer's counter. gapless by design
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  model_id        TEXT,
  thinking        TEXT,
  agent_of        TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  agent_name      TEXT,
  notebook_folder TEXT,
  access          TEXT CHECK (access IN ('read', 'work')),
  title_auto      INTEGER NOT NULL DEFAULT 0,
  archived_at     TEXT,
  notes_off       TEXT NOT NULL DEFAULT '[]',
  kind            TEXT NOT NULL DEFAULT 'chat'
                    CHECK (kind IN ('chat', 'mission', 'agent', 'background')),
  trashed_at      TEXT,                     -- deleted: hidden, restorable, never removed
  auto_context    INTEGER NOT NULL DEFAULT 1,
  agent_cli       TEXT,
  guard_off       INTEGER NOT NULL DEFAULT 0  -- Bastet skips this conversation
);
CREATE INDEX conversations_pinned  ON conversations(pinned DESC, updated_at DESC);
CREATE INDEX conversations_updated ON conversations(updated_at DESC);

CREATE TABLE messages (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  parent_id       TEXT REFERENCES messages(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
  content         TEXT NOT NULL DEFAULT '',
  reasoning       TEXT,
  ordinal         INTEGER NOT NULL DEFAULT 0,
  preferred_child_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
  turn_id         TEXT,
  author          TEXT NOT NULL DEFAULT 'user'
                    CHECK (author IN ('user','model','intercom')),
  intercom_id     INTEGER REFERENCES intercom(id),
  created_at      TEXT NOT NULL,
  finish_reason   TEXT,
  meta            TEXT,
  trashed_at      TEXT
);
CREATE INDEX messages_conv   ON messages(conversation_id, created_at);
CREATE INDEX messages_parent ON messages(parent_id, ordinal);
CREATE INDEX messages_turn   ON messages(turn_id);

CREATE VIRTUAL TABLE messages_fts USING fts5(
  content, content='messages', content_rowid='rowid',
  tokenize='porter unicode61'
);
CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER messages_fts_au AFTER UPDATE OF content ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
  INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;

-- the context text a reply was built on (turns/context)
CREATE TABLE message_context (
  message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  text       TEXT NOT NULL,
  report     TEXT NOT NULL DEFAULT '{}'
);

-- ---- turns, tool calls, jobs, work ---------------------------------------------------

CREATE TABLE turns (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  root_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
  state           TEXT NOT NULL CHECK (state IN
                    ('queued','preparing','streaming','tools',
                     'done','aborted','failed','interrupted','capped')),
  reason          TEXT,
  abort_requested INTEGER NOT NULL DEFAULT 0,
  worker_id       TEXT,
  origin          TEXT NOT NULL DEFAULT 'user' CHECK (origin IN ('user','intercom')),
  iterations      INTEGER NOT NULL DEFAULT 0,
  backend_id      TEXT,
  model           TEXT,
  created_at      TEXT NOT NULL,
  finished_at     TEXT,
  continues       INTEGER NOT NULL DEFAULT 0,
  cut             TEXT,
  reason_code     TEXT,
  quiet           INTEGER NOT NULL DEFAULT 0,
  again           TEXT,
  cap_base        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX turns_active ON turns(conversation_id, state);
CREATE INDEX turns_queue  ON turns(state, created_at);

CREATE TABLE tool_calls (
  id                TEXT PRIMARY KEY,
  message_id        TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  result_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
  name              TEXT NOT NULL,
  arguments         TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN
                      ('pending','running','ok','error','gated','cancelled','detached',
                       'interrupted')),
  display           TEXT,
  withheld_output   TEXT,   -- finished after cancel: kept for you, never sent to her
  job_id            TEXT,
  started_at        TEXT,
  finished_at       TEXT,
  mutating          INTEGER,  -- 1/0 from the tool's declaration; NULL: not recorded
  mode              TEXT      -- blocking | detach | suspend; NULL: not recorded
);
CREATE INDEX tool_calls_job     ON tool_calls(job_id);
CREATE INDEX tool_calls_message ON tool_calls(message_id);
CREATE INDEX tool_calls_name    ON tool_calls(name, started_at DESC);

CREATE TABLE tool_overrides (
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  target          TEXT NOT NULL,    -- "notebook" or "notebook__write"
  enabled         INTEGER NOT NULL,
  client_seq      INTEGER NOT NULL DEFAULT 0,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (conversation_id, target)
);

-- steps that outlive their turn (jobs/)
CREATE TABLE jobs (
  id                TEXT PRIMARY KEY,
  conversation_id   TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  origin_turn_id    TEXT REFERENCES turns(id) ON DELETE SET NULL,
  tool_call_id      TEXT REFERENCES tool_calls(id) ON DELETE SET NULL,
  tool_name         TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN
                      ('running','done','failed','cancelled','timeout','interrupted')),
  result            TEXT,
  created_at        TEXT NOT NULL,
  finished_at       TEXT,
  progress          TEXT,
  stopped_by        TEXT CHECK (stopped_by IN ('user','celine','turn') OR stopped_by IS NULL)
);
CREATE INDEX jobs_state ON jobs(state, created_at DESC);

-- the work queue of background model jobs (work.py)
CREATE TABLE work (
  id              INTEGER PRIMARY KEY,
  kind            TEXT NOT NULL,
  priority        INTEGER NOT NULL DEFAULT 0,
  state           TEXT NOT NULL DEFAULT 'queued' CHECK (state IN
                    ('queued', 'running', 'done', 'deferred', 'failed')),
  payload         TEXT NOT NULL DEFAULT '{}',
  not_before      TEXT,
  lease_worker    TEXT,
  lease_until     TEXT,
  tries           INTEGER NOT NULL DEFAULT 0,
  error           TEXT,
  conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
  created_at      TEXT NOT NULL,
  started_at      TEXT,
  finished_at     TEXT
);
CREATE INDEX work_kind ON work(kind, conversation_id, state);
CREATE INDEX work_open ON work(state, priority DESC, created_at);

-- ---- context: what she carries and compaction ----------------------------------------

CREATE TABLE compactions (
  id                 TEXT PRIMARY KEY,
  conversation_id    TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  through_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  trigger            TEXT NOT NULL CHECK (trigger IN
                       ('auto','background','manual','emergency','turn')),
  covers_messages    INTEGER NOT NULL,        -- messages no longer sent, in total
  text               TEXT NOT NULL DEFAULT '',
  tokens             INTEGER NOT NULL DEFAULT 0,   -- of the text
  tokens_before      INTEGER,                 -- request estimate before / after
  tokens_after       INTEGER,
  focus              TEXT,                    -- /compact <focus>
  model              TEXT,
  created_at         TEXT NOT NULL
);
CREATE INDEX compactions_conv ON compactions(conversation_id, created_at);

CREATE TABLE context_loaded (
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  kind            TEXT NOT NULL CHECK (kind IN ('note', 'skill')),
  name            TEXT NOT NULL,
  touched         INTEGER NOT NULL,
  PRIMARY KEY (conversation_id, kind, name)
);

CREATE TABLE context_state (
  conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
  aged_through    TEXT,
  turns           INTEGER NOT NULL DEFAULT 0,
  updated_at      TEXT NOT NULL
);

-- her plan, one per conversation (plans/)
CREATE TABLE plans (
  conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
  text            TEXT NOT NULL DEFAULT '',
  steps           TEXT NOT NULL DEFAULT '[]',
  waiting         INTEGER NOT NULL DEFAULT 0,
  updated_at      TEXT NOT NULL
);

-- ---- files: blobs, attachments, images and music -------------------------------------

CREATE TABLE blobs (
  sha256     TEXT PRIMARY KEY,
  bytes      INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  width      INTEGER,
  height     INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE media_batches (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN ('images','music')),
  conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
  tool_call_id    TEXT REFERENCES tool_calls(id) ON DELETE CASCADE,
  state           TEXT NOT NULL CHECK (state IN ('running','done','stopped')),
  items           TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  finished_at     TEXT
);
CREATE INDEX media_batches_conversation ON media_batches(conversation_id, created_at);
CREATE INDEX media_batches_kind         ON media_batches(kind, created_at);

CREATE TABLE attachments (
  id              TEXT PRIMARY KEY,
  message_id      TEXT REFERENCES messages(id) ON DELETE CASCADE,
  sha256          TEXT NOT NULL REFERENCES blobs(sha256),
  filename        TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('image','text','binary','audio')),
  state           TEXT NOT NULL CHECK (state IN ('uploading','ready','failed')),
  created_at      TEXT NOT NULL,
  description     TEXT,
  described_by    TEXT,
  described_for   TEXT,
  tool_call_id    TEXT REFERENCES tool_calls(id) ON DELETE CASCADE,
  batch_id        TEXT REFERENCES media_batches(id) ON DELETE CASCADE,
  box_path        TEXT
);
CREATE INDEX attachments_batch     ON attachments(batch_id);
CREATE INDEX attachments_message   ON attachments(message_id);
CREATE INDEX attachments_state     ON attachments(state, created_at);
CREATE INDEX attachments_tool_call ON attachments(tool_call_id);

-- ---- Bastet, the guard (guard/) ------------------------------------------------------

CREATE TABLE guard_decisions (
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
                    'failed','unreachable','found','closed','asked','approved','denied',
                    'passed')),
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
CREATE INDEX guard_decisions_call    ON guard_decisions(tool_call_id);
CREATE INDEX guard_decisions_message ON guard_decisions(message_id);
CREATE INDEX guard_decisions_turn    ON guard_decisions(turn_id);

CREATE TABLE guard_passes (
  id              INTEGER PRIMARY KEY,
  rule            TEXT NOT NULL,
  conversation_id TEXT,
  until           TEXT NOT NULL,
  by              TEXT NOT NULL,
  why             TEXT,
  created_at      TEXT NOT NULL,
  told_at         TEXT
);
CREATE INDEX guard_passes_until ON guard_passes(until);

CREATE TABLE freezes (
  id              INTEGER PRIMARY KEY,
  at              TEXT NOT NULL,
  by              TEXT NOT NULL CHECK (by IN ('guard','user')),
  why             TEXT NOT NULL,
  conversation_id TEXT,
  turn_id         TEXT,
  decision_id     INTEGER,
  unfrozen_at     TEXT,
  unfrozen_by     TEXT CHECK (unfrozen_by IS NULL OR unfrozen_by IN ('user','bastet')),
  precedents      TEXT,
  asked_at        TEXT,
  asked_id        INTEGER
);

-- ---- intercom: messages between everyone (intercom/) ---------------------------------

CREATE TABLE intercom (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at   TEXT NOT NULL,
  from_addr    TEXT NOT NULL,
  to_addr      TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('message','brief','job','report','trigger','notice')),
  subject      TEXT,
  text         TEXT NOT NULL DEFAULT '',
  data         TEXT NOT NULL DEFAULT '{}',
  wake         INTEGER NOT NULL DEFAULT 1,
  due_at       TEXT,
  until_at     TEXT,
  reply_to     INTEGER REFERENCES intercom(id),
  thread       INTEGER REFERENCES intercom(id),
  cancelled_at TEXT
);

CREATE TABLE intercom_deliveries (
  intercom_id     INTEGER NOT NULL REFERENCES intercom(id),
  to_addr         TEXT NOT NULL,
  conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
  state           TEXT NOT NULL CHECK (state IN ('pending','delivered','read','cancelled')),
  message_id      TEXT REFERENCES messages(id) ON DELETE SET NULL,
  turn_id         TEXT REFERENCES turns(id) ON DELETE SET NULL,
  delivered_at    TEXT,
  PRIMARY KEY (intercom_id, to_addr)
);
CREATE INDEX intercom_deliveries_turn    ON intercom_deliveries(turn_id);
CREATE INDEX intercom_deliveries_waiting ON intercom_deliveries(conversation_id, state);

CREATE TABLE intercom_tokens (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  hash        BLOB NOT NULL,
  created_at  TEXT NOT NULL,
  revoked_at  TEXT
);
CREATE UNIQUE INDEX intercom_tokens_active ON intercom_tokens(name) WHERE revoked_at IS NULL;

-- her bug reports, each an Intercom thread (bugreports/)
CREATE TABLE bugreports (
  id              INTEGER PRIMARY KEY,
  conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  title           TEXT NOT NULL,
  severity        TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
  what            TEXT NOT NULL,
  place           TEXT NOT NULL DEFAULT '',
  wish            TEXT NOT NULL DEFAULT '',
  state           TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
  created_at      TEXT NOT NULL,
  closed_at       TEXT,
  thread          INTEGER REFERENCES intercom(id) ON DELETE SET NULL
);
CREATE INDEX bugreports_state  ON bugreports(state, created_at);
CREATE INDEX bugreports_thread ON bugreports(thread);

-- ---- missions and routines -----------------------------------------------------------

CREATE TABLE missions (
  conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  terminal        TEXT NOT NULL,
  report_to       TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  until           TEXT,
  wake_id         TEXT,
  status          TEXT NOT NULL CHECK (status IN ('active','needs_you','done','stopped')),
  note            TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  brief           TEXT NOT NULL DEFAULT '',
  artifact        TEXT,
  check_in        INTEGER                 -- minutes between check-ins; NULL: none
);

-- a schedule's runs (schedules/runs.py)
CREATE TABLE runs (
  id              TEXT PRIMARY KEY,
  schedule        TEXT NOT NULL,
  label           TEXT NOT NULL,
  slot            TEXT NOT NULL,
  night           TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN
                    ('waiting', 'running', 'done', 'failed', 'skipped', 'stopped')),
  reason          TEXT NOT NULL DEFAULT '',
  conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  result          TEXT,
  error           TEXT,
  created_at      TEXT NOT NULL,
  started_at      TEXT,
  finished_at     TEXT,
  started_by      TEXT NOT NULL DEFAULT 'clock'
                    CHECK (started_by IN ('clock', 'user', 'her')),
  UNIQUE (schedule, slot)
);
CREATE INDEX runs_night ON runs(night);
-- one run at a time (schedules/runs.py)
CREATE UNIQUE INDEX runs_one_running ON runs(state) WHERE state = 'running';

-- ---- her terminals, browser tabs, pages ----------------------------------------------

CREATE TABLE terminals (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  owner      TEXT NOT NULL,
  may_type   INTEGER NOT NULL DEFAULT 0,
  opened_by  TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  opened_at  TEXT NOT NULL,
  ended_at   TEXT,
  ended_by   TEXT CHECK (ended_by IN ('exit', 'user', 'reset', 'gone', 'done', 'idle')
                         OR ended_by IS NULL),
  exit       INTEGER,
  trashed_at TEXT
);
CREATE INDEX terminals_open      ON terminals(name, ended_at);
CREATE INDEX terminals_opened_by ON terminals(opened_by) WHERE ended_at IS NULL;

CREATE TABLE tabs (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  opened_by TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  opened_at TEXT NOT NULL,
  used_at   TEXT NOT NULL,
  url       TEXT,
  closed_at TEXT
);
CREATE UNIQUE INDEX tabs_open ON tabs(name) WHERE closed_at IS NULL;

CREATE TABLE artifacts (
  id              TEXT PRIMARY KEY,                      -- 6 characters a-z 0-9
  kind            TEXT NOT NULL CHECK (kind IN ('html', 'markdown')),
  conversation_id TEXT,
  tool_call_id    TEXT,
  created_at      TEXT NOT NULL,
  trashed_at      TEXT
);
CREATE INDEX artifacts_conversation ON artifacts(conversation_id);

CREATE TABLE artifact_versions (
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  n           INTEGER NOT NULL,
  title       TEXT NOT NULL,
  content     TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  PRIMARY KEY (artifact_id, n)
);

CREATE TABLE artifact_errors (
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  n           INTEGER NOT NULL,
  at          TEXT NOT NULL,
  message     TEXT NOT NULL,
  line        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX artifact_errors_version ON artifact_errors(artifact_id, n);

CREATE TABLE artifact_data (
  artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),
  json        TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- ---- shares: a server or files of her shellbox, shown through /share/<token>/ ---------

CREATE TABLE shares (
  token           TEXT PRIMARY KEY,                      -- 24 characters a-z 0-9: its id and its address
  title           TEXT NOT NULL,
  port            INTEGER,                               -- a server she runs in her box, or
  path            TEXT,                                  -- a file or folder in her home
  folder          INTEGER NOT NULL DEFAULT 0,
  reset           TEXT NOT NULL,                         -- the box's last reset when shared
  conversation_id TEXT,
  public          INTEGER NOT NULL DEFAULT 0,            -- 1: anyone with the link; else the owner, logged in
  secret          TEXT NOT NULL,                         -- the owner's cookie to it (GET /api/shares/<token>/open)
  created_at      TEXT NOT NULL,
  ends_at         TEXT NOT NULL,                         -- its time limit, passive: nothing runs at it
  ended_at        TEXT,                                  -- she, the owner or a reset of her box ended it
  CHECK ((port IS NULL) != (path IS NULL))
);

-- ---- knowledge bases: who is who, MCP tools ------------------------------------------

CREATE TABLE identities (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  about      TEXT NOT NULL DEFAULT '',
  aliases    TEXT NOT NULL DEFAULT '[]'
               CHECK (json_valid(aliases) AND json_type(aliases) = 'array')
);

CREATE TABLE identity_members (
  kb          TEXT NOT NULL,
  person_id   INTEGER NOT NULL,
  identity_id INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
  source      TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user','celine')),
  created_at  TEXT NOT NULL,
  PRIMARY KEY (kb, person_id)
);
CREATE INDEX identity_members_identity ON identity_members(identity_id);

CREATE TABLE mcp_tools (
  server      TEXT NOT NULL,
  name        TEXT NOT NULL,
  description TEXT NOT NULL,
  schema      TEXT NOT NULL,
  seen_at     TEXT NOT NULL,
  annotations TEXT,
  PRIMARY KEY (server, name)
);
