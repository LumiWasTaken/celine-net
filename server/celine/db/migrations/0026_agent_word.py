"""The agent is `agent` in the database, where it was `her`, and its pronouns are no longer
placeholders in a text:

- the table `her` is `agent`;
- every stored value that named the agent `her` names it `agent`: a text's History name
  (`of`, agent:persona, agent:talk, agent:voice) and its writer (`by`), a mood's `by`, a
  run's `started_by` (the table is built again for its CHECK), a plan step's `by`, and in
  log.db the global feed's agent.changed and agent.hold and the home `agent` in an
  event's data;
- {{she}}, {{her}}, {{herself}} and {{them}} (and {{She}}, {{Her}}, {{Herself}},
  {{Them}}) become {{name}} in every saved text: the agent's persona and talk, every
  version in History, Bastet's edited texts (kv bastet:<field>), the user's prompt
  edits (prompts/<id>.md) and the Routines (settings/schedules/*.toml and *.md), beside
  celine.db.

Run again, it writes nothing."""
from __future__ import annotations

import json
import re
from pathlib import Path

PLACEHOLDER = re.compile(r"\{\{(?:she|her|herself|them|She|Her|Herself|Them)\}\}")
RUNS = """CREATE TABLE runs_new (
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
                    CHECK (started_by IN ('clock', 'user', 'agent')),
  again_of        TEXT REFERENCES runs(id) ON DELETE SET NULL,
  UNIQUE (schedule, slot)
)"""
COLUMNS = ("id, schedule, label, slot, night, state, reason, conversation_id, result, error, "
           "created_at, started_at, finished_at, started_by, again_of")


def named(text: str) -> str:
    return PLACEHOLDER.sub("{{name}}", text)


def _table(conn, name: str) -> str | None:
    r = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                     (name,)).fetchone()
    return r[0] if r else None


def _texts(conn) -> None:
    if _table(conn, "her") is not None:
        conn.execute("ALTER TABLE her RENAME TO agent")
    row = conn.execute("SELECT persona, talk FROM agent WHERE id = 1").fetchone()
    if row is not None:
        conn.execute("UPDATE agent SET persona=?, talk=? WHERE id = 1",
                     (named(row[0]), None if row[1] is None else named(row[1])))
    conn.execute("UPDATE text_versions SET of = 'agent:' || substr(of, 5) "
                 "WHERE of LIKE 'her:%'")
    conn.execute("UPDATE text_versions SET by = 'agent' WHERE by = 'her'")
    for vid, text in conn.execute("SELECT id, text FROM text_versions "
                                  "WHERE text LIKE '%{{%'").fetchall():
        conn.execute("UPDATE text_versions SET text=? WHERE id=?", (named(text), vid))
    for k, v in conn.execute("SELECT k, v FROM kv WHERE k LIKE 'bastet:%'").fetchall():
        conn.execute("UPDATE kv SET v=? WHERE k=?", (named(v), k))
    conn.execute("UPDATE moods SET by = 'agent' WHERE by = 'her'")
    for cid, raw in conn.execute("SELECT conversation_id, steps FROM plans").fetchall():
        steps = [{**s, "by": "agent"} if s.get("by") == "her" else s for s in json.loads(raw)]
        conn.execute("UPDATE plans SET steps=? WHERE conversation_id=?",
                     (json.dumps(steps, ensure_ascii=False), cid))


def _runs(conn) -> None:
    if "'her'" not in (_table(conn, "runs") or ""):
        return
    conn.execute(RUNS)
    picked = COLUMNS.replace("started_by", "replace(started_by, 'her', 'agent')")
    conn.execute(f"INSERT INTO runs_new({COLUMNS}) SELECT {picked} FROM runs")
    conn.execute("DROP TABLE runs")
    conn.execute("ALTER TABLE runs_new RENAME TO runs")
    conn.execute("CREATE INDEX runs_night ON runs(night)")
    conn.execute("CREATE UNIQUE INDEX runs_one_running ON runs(state) WHERE state = 'running'")


def _log(conn, db_path: Path) -> None:
    log_db = db_path.parent / "log.db"
    if not log_db.is_file():
        return
    conn.execute("ATTACH DATABASE ? AS old_log", (str(log_db),))
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE old_log.global_events SET type = 'agent.' || substr(type, 5) "
                     "WHERE type IN ('her.changed', 'her.hold')")
        for table in ("events", "global_events"):
            conn.execute(f"UPDATE old_log.{table} SET data = replace(data, "
                         "'\"home\": \"her\"', '\"home\": \"agent\"') "
                         "WHERE data LIKE '%\"home\": \"her\"%'")
        conn.execute("COMMIT")
    finally:
        conn.execute("DETACH DATABASE old_log")


def _files(db_path: Path) -> None:
    root = db_path.parent
    for path in [*(root / "prompts").glob("*.md"),
                 *(root / "settings" / "schedules").glob("*.toml"),
                 *(root / "settings" / "schedules").glob("*.md")]:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if (new := named(text)) != text:
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(new)
            tmp.replace(path)


def up(conn, db_path) -> None:
    db_path = Path(db_path)
    conn.execute("BEGIN")
    _texts(conn)
    _runs(conn)
    conn.execute("COMMIT")
    _log(conn, db_path)
    _files(db_path)
