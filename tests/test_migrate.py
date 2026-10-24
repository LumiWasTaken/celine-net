"""Migrations: 0001_schema is the whole schema of celine.db, log/0001_schema that of
log.db, notebook/0001_schema that of the notebook's index; later files apply on top in
order; a database older than its first file is refused."""
from __future__ import annotations

import shutil
import sqlite3

import pytest

import celine.db.migrate as m
from celine.db.migrate import MIGRATIONS_DIR, log_path, migrate, migrate_all, notebook_db

EXPECTED_TABLES = {
    "conversations", "messages", "tool_calls", "turns", "jobs",
    "tool_overrides", "blobs", "attachments", "mcp_tools", "kv",
    "identities", "identity_members", "terminals",
    "missions", "messages_fts", "artifacts", "artifact_versions",
    "artifact_errors", "artifact_data", "shares", "intercom", "intercom_deliveries",
    "message_context", "media_batches", "context_loaded", "context_state", "runs", "moods",
    "guard_decisions", "her", "text_versions", "compactions", "freezes", "guard_passes", "work",
    "tabs", "bugreports", "plans",
}
LOG_TABLES = {"events", "global_events", "raw_responses", "requests", "terminal_commands"}
NOTEBOOK_TABLES = {"notebook_index", "notebook_fts"}
LATEST = 2


def _version(db) -> int:
    c = sqlite3.connect(db)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def _tables(db) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def test_a_fresh_database_gets_every_table_and_its_words_are_searchable(tmp_path):
    db = tmp_path / "celine.db"
    assert migrate_all(db) == LATEST and _version(db) == LATEST
    tables = _tables(db)
    assert EXPECTED_TABLES <= tables and not (LOG_TABLES | NOTEBOOK_TABLES) & tables
    logs = _tables(log_path(db))
    assert LOG_TABLES <= logs and not (EXPECTED_TABLES | NOTEBOOK_TABLES) & logs
    notes = _tables(notebook_db(db))
    assert NOTEBOOK_TABLES <= notes and not (EXPECTED_TABLES | LOG_TABLES) & notes
    for f in (log_path(db), notebook_db(db)):
        assert sqlite3.connect(f).execute("PRAGMA user_version").fetchone() == (1,)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO conversations(id, title, next_seq, created_at, updated_at) "
              "VALUES('c1', 't', 1, 'x', 'x')")
    c.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) "
              "VALUES('m1', 'c1', 'user', 'find the lighthouse photo', 'x')")
    assert c.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH "
                     "'lighthouse'").fetchone() == (1,)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.commit()
    c.close()
    c = sqlite3.connect(db)
    c.execute(f"PRAGMA user_version = {LATEST + 1}")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match="only knows"):
        migrate(db)                     # a database from a newer build is left alone


def test_the_schema_never_runs_twice_and_an_older_database_is_refused(tmp_path, monkeypatch):
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0002_schema.sql").write_text("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);\n")
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "celine.db"
    assert migrate(db) == 2
    (d / "0002_schema.sql").write_text("this is not sql;")
    assert migrate(db) == 2, "the schema never runs twice"
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version = 1")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match="older than 0002_schema.sql"):
        migrate(db)


def test_later_migrations_apply_in_order_on_top_of_the_schema(tmp_path, monkeypatch):
    d = tmp_path / "migrations"
    d.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_schema.sql", d)
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "celine.db"
    assert migrate(db) == 1
    (d / "0003_second.sql").write_text("ALTER TABLE later ADD COLUMN b TEXT;\n")
    (d / "0002_first.sql").write_text("CREATE TABLE later(a TEXT);\n")
    assert migrate(db) == 3 and _version(db) == 3
    c = sqlite3.connect(db)
    assert [r[1] for r in c.execute("PRAGMA table_info(later)")] == ["a", "b"]
    c.close()


def test_a_migration_that_breaks_foreign_keys_is_rolled_back(tmp_path, monkeypatch):
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_init.sql").write_text(
        "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);\n"
        "CREATE TABLE a(id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE b(a_id INTEGER REFERENCES a(id));\n")
    (d / "0002_broken.sql").write_text("INSERT INTO b VALUES (7);\n")
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "x.db"
    for _ in range(2):
        with pytest.raises(RuntimeError, match="broken foreign key"):
            migrate(db)
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA user_version").fetchone() == (1,)
    assert c.execute("SELECT count(*) FROM b").fetchone() == (0,)


def test_a_run_has_one_try(tmp_path):
    db = tmp_path / "celine.db"
    assert migrate(db) == LATEST
    c = sqlite3.connect(db)
    cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    assert not cols & {"attempt", "attempts", "not_before"}
    assert {"conversation_id", "started_at", "finished_at"} <= cols
    c.close()


def test_0002_drops_the_message_trash_and_keeps_what_followed(tmp_path, monkeypatch):
    d = tmp_path / "migrations"
    d.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_schema.sql", d)
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "celine.db"
    assert migrate(db) == 1
    c = sqlite3.connect(db)
    c.execute("INSERT INTO conversations(id, title, next_seq, created_at, updated_at, "
              "active_leaf_id) VALUES('c', 't', 1, 'x', 'x', 'm4')")
    for mid, parent, trashed in (("m1", None, None), ("m2", "m1", "x"), ("m3", "m2", "x"),
                                 ("m4", "m3", None), ("t1", None, "x")):
        c.execute("INSERT INTO messages(id, conversation_id, parent_id, role, content, "
                  "created_at, trashed_at) VALUES(?, 'c', ?, 'user', ?, 'x', ?)",
                  (mid, parent, mid, trashed))
    c.commit()
    c.close()
    shutil.copy(MIGRATIONS_DIR / "0002_no_message_trash.sql", d)
    assert migrate(db) == 2
    c = sqlite3.connect(db)
    assert c.execute("SELECT id, parent_id FROM messages ORDER BY id").fetchall() == \
        [("m1", None), ("m4", "m1")], "the trashed ones go, what followed them stays"
    assert "trashed_at" not in {r[1] for r in c.execute("PRAGMA table_info(messages)")}
    assert c.execute("SELECT active_leaf_id FROM conversations").fetchone() == ("m4",)
    c.close()
