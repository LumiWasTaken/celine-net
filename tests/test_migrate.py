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
    "message_context", "media_batches", "context_state", "runs", "moods",
    "guard_decisions", "her", "text_versions", "compactions", "freezes", "guard_passes", "work",
    "tabs", "bugreports", "plans", "push_subscriptions",
}
LOG_TABLES = {"events", "global_events", "raw_responses", "requests", "terminal_commands"}
NOTEBOOK_TABLES = {"notebook_index", "notebook_fts"}
LATEST = 13


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
    for f, n in ((log_path(db), 2), (notebook_db(db), 1)):
        assert sqlite3.connect(f).execute("PRAGMA user_version").fetchone() == (n,)
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
    assert "tries" not in {r[1] for r in c.execute("PRAGMA table_info(work)")}
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


def test_a_file_is_copied_before_a_migration_changes_it_and_the_last_copies_kept(
        tmp_path, monkeypatch):
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_init.sql").write_text("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);\n")
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "x.db"
    assert migrate(db) == 1
    assert not list(tmp_path.glob("x.db.pre-*")), "a new file has nothing to keep"
    for n in range(2, 7):
        (d / f"{n:04d}_step.sql").write_text(f"INSERT INTO kv VALUES('{n}', 'x');\n")
        assert migrate(db) == n
    assert migrate(db) == 6, "nothing to apply, nothing copied"
    assert sorted(p.name for p in tmp_path.glob("x.db.pre-*")) == \
        [f"x.db.pre-{n}.bak" for n in range(6 - m.COPIES_KEPT + 1, 7)]
    c = sqlite3.connect(tmp_path / "x.db.pre-6.bak")
    assert c.execute("PRAGMA user_version").fetchone() == (5,)
    assert [r[0] for r in c.execute("SELECT k FROM kv ORDER BY k")] == ["2", "3", "4", "5"]


def test_a_retry_after_a_failed_upgrade_keeps_the_copy_from_before_it(tmp_path, monkeypatch):
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_init.sql").write_text("CREATE TABLE kv(k TEXT PRIMARY KEY);\nINSERT INTO kv VALUES('a');\n")
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "x.db"
    assert migrate(db) == 1
    (d / "0002_drop.sql").write_text("DELETE FROM kv;\n")
    (d / "0003_bad.sql").write_text("INSERT INTO nope VALUES(1);\n")
    for _ in range(2):
        with pytest.raises(sqlite3.OperationalError):
            migrate(db)
    c = sqlite3.connect(tmp_path / "x.db.pre-3.bak")
    assert c.execute("SELECT k FROM kv").fetchall() == [("a",)]


def test_0004_gives_each_sub_agent_call_the_link_of_its_conversation(tmp_path, monkeypatch):
    """A sub-agent's call names where its conversation opens (homes.of), as its display
    does from now on."""
    import json
    from celine import homes
    d = tmp_path / "migrations"
    d.mkdir()
    for f in ("0001_schema.sql", "0002_no_message_trash.sql", "0003_turn_end_hooks.sql"):
        shutil.copy(MIGRATIONS_DIR / f, d)
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "celine.db"
    assert migrate(db) == 3
    c = sqlite3.connect(db)
    for cid, kind, agent_of, key, at in (
            ("chat", "chat", None, None, "x"), ("mis", "mission", None, None, "x"),
            ("run", "background", None, "run:r1:1", "x"),
            ("task", "background", None, None, "2026-10-21T10:00:00+00:00"),
            ("s-chat", "agent", "chat", None, "x"), ("s-mis", "agent", "mis", None, "x"),
            ("s-run", "agent", "run", None, "x"), ("s-task", "agent", "task", None, "x"),
            ("br", "background", None, "bridge:whatsapp", "x"), ("s-br", "agent", "br", None, "x")):
        c.execute("INSERT INTO conversations(id, kind, agent_of, trigger_key, created_at, "
                  "updated_at) VALUES(?, ?, ?, ?, ?, 'x')", (cid, kind, agent_of, key, at))
    c.execute("INSERT INTO runs(id, schedule, label, slot, night, state, conversation_id, "
              "created_at) VALUES('r1', 'diary', 'Diary', 's', '2026-10-22', 'done', 'run', 'x')")
    c.execute("INSERT INTO messages(id, conversation_id, role, created_at) "
              "VALUES('m', 'chat', 'assistant', 'x')")
    subs = ("s-chat", "s-mis", "s-run", "s-task", "gone")
    for i, sub in enumerate((*subs, "s-br")):
        c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state, display) "
                  "VALUES(?, 'm', 'sub_agents__start', '{}', 'ok', ?)",
                  (f"c{i}", json.dumps({"name": "helper", "conversation_id": sub})))
    c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state, display) "
              "VALUES('other', 'm', 'notebook__read', '{}', 'ok', ?)",
              (json.dumps({"conversation_id": "chat"}),))
    c.commit()
    c.close()
    shutil.copy(next(MIGRATIONS_DIR.glob("0004_*.sql")), d)
    assert migrate(db) == 4
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    shown = [json.loads(r[0]) for r in c.execute(
        "SELECT display FROM tool_calls WHERE id != 'other'")]
    got = {x["conversation_id"]: x.get("link", "-") for x in shown}
    assert got.pop("s-br") == "/chat/s-br", "a bridged chat's (a chat after 0005) opens with her"
    assert got == {sub: homes.of(c, sub)["link"] for sub in subs}
    assert got["s-run"] == "/routines/2026-10-22/task/s-run" and got["gone"] is None
    assert "link" not in json.loads(c.execute(
        "SELECT display FROM tool_calls WHERE id='other'").fetchone()[0]), "only hers"
    c.close()


def test_0005_makes_the_bridged_chats_plain_chats_and_drops_what_only_the_bridge_read(
        tmp_path, monkeypatch):
    """The bridge is gone: its conversations are chats like any other, and the marks it
    left in intercom rows and image batches go."""
    import json
    d = tmp_path / "migrations"
    d.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("000[1-4]_*.sql")):
        shutil.copy(f, d)
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "celine.db"
    assert migrate(db) == 4
    c = sqlite3.connect(db)
    for cid, kind, key in (("wa", "background", "bridge:whatsapp"),
                           ("run", "background", "run:r1:1"), ("chat", "chat", None)):
        c.execute("INSERT INTO conversations(id, title, kind, trigger_key, created_at, "
                  "updated_at) VALUES(?, ?, ?, ?, 'x', 'x')", (cid, cid, kind, key))
    for i, data in enumerate(({"via": "whatsapp"}, {"via": "whatsapp", "sent": ["K1"]},
                              {"urgent": True}, {"source": "x"})):
        c.execute("INSERT INTO intercom(id, created_at, from_addr, to_addr, kind, data) "
                  "VALUES(?, 'x', 'user', 'agent:wa', 'message', ?)", (i + 1, json.dumps(data)))
    items = [{"state": "done", "bridged": True}, {"state": "failed"}]
    c.execute("INSERT INTO media_batches(id, kind, conversation_id, state, items, created_at) "
              "VALUES('b1', 'images', 'wa', 'done', ?, 'x')", (json.dumps(items),))
    c.commit()
    c.close()
    shutil.copy(next(MIGRATIONS_DIR.glob("0005_*.sql")), d)
    assert migrate(db) == 5
    c = sqlite3.connect(db)
    assert c.execute("SELECT id, kind, trigger_key FROM conversations ORDER BY id").fetchall() \
        == [("chat", "chat", None), ("run", "background", "run:r1:1"), ("wa", "chat", None)]
    assert [json.loads(r[0]) for r in c.execute("SELECT data FROM intercom ORDER BY id")] \
        == [{}, {}, {}, {"source": "x"}]
    assert json.loads(c.execute("SELECT items FROM media_batches").fetchone()[0]) == \
        [{"state": "done"}, {"state": "failed"}]
    c.close()


def test_a_python_step_runs_up_with_no_transaction_open_once(tmp_path):
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_init.sql").write_text("CREATE TABLE old(a TEXT);\nINSERT INTO old VALUES ('x');\n")
    (d / "0002_move.py").write_text(
        "def up(conn, db_path):\n"
        "    assert not conn.in_transaction\n"
        "    other = db_path.with_name('other.db')\n"
        "    conn.execute('ATTACH ? AS o', (str(other),))\n"
        "    if conn.execute(\"SELECT 1 FROM main.sqlite_master WHERE name = 'old'\").fetchone():\n"
        "        conn.execute('BEGIN')\n"
        "        conn.execute('CREATE TABLE IF NOT EXISTS o.moved(a TEXT)')\n"
        "        conn.execute('INSERT INTO o.moved SELECT a FROM main.old')\n"
        "        conn.execute('DROP TABLE main.old')\n"
        "        conn.execute('COMMIT')\n"
        "    conn.execute('DETACH o')\n")
    db = tmp_path / "x.db"
    assert migrate(db, d) == 2 and _version(db) == 2
    assert "old" not in _tables(db)
    c = sqlite3.connect(tmp_path / "other.db")
    assert c.execute("SELECT a FROM moved").fetchall() == [("x",)]
    c.close()
    # a crash between its commit and the bump runs it again: nothing moves twice
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version = 1")
    c.commit()
    c.close()
    assert migrate(db, d) == 2 and _version(db) == 2
    c = sqlite3.connect(tmp_path / "other.db")
    assert c.execute("SELECT count(*) FROM moved").fetchone() == (1,)
    c.close()


def test_0006_gives_each_call_with_its_own_files_a_files_block(tmp_path, monkeypatch):
    """Old rows read like new ones: a shell look's files leave display.attachments for
    the block; a browser shot its display names stays out of it; a call without files
    of its own is not touched. Run twice, the same."""
    import json
    d = tmp_path / "migrations"
    d.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("000[1-5]_*.sql")):
        shutil.copy(f, d)
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "celine.db"
    assert migrate(db) == 5
    c = sqlite3.connect(db)
    c.execute("INSERT INTO conversations(id, kind, created_at, updated_at) "
              "VALUES('c', 'chat', 'x', 'x')")
    c.execute("INSERT INTO messages(id, conversation_id, role, created_at) "
              "VALUES('m', 'c', 'assistant', 'x')")
    look = {"paths": ["a.png"], "attachments": [{"id": "a1", "filename": "a.png"}]}
    shot = {"tab": "main", "shot": {"id": "a2"}}
    for cid, name, display in (("look", "shell__look", look), ("shot", "browser__look", shot),
                               ("send", "shell__send", {"attachments": [{"id": "a3"}]})):
        c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state, display) "
                  "VALUES(?, 'm', ?, '{}', 'ok', ?)", (cid, name, json.dumps(display)))
    c.execute("INSERT INTO blobs(sha256, bytes, media_type, width, height, created_at) "
              "VALUES('s1', 5, 'image/png', 4, 3, 'x')")
    for aid, call, mid in (("a1", "look", None), ("a2", "shot", None), ("a3", None, "m")):
        c.execute("INSERT INTO attachments(id, message_id, tool_call_id, sha256, filename, "
                  "kind, state, created_at) VALUES(?, ?, ?, 's1', 'a.png', 'image', 'ready', "
                  "'x')", (aid, mid, call))
    c.commit()
    c.close()
    shutil.copy(next(MIGRATIONS_DIR.glob("0006_*.py")), d)
    for _ in range(2):
        assert migrate(db) == 6
        c = sqlite3.connect(db)
        shown = {r[0]: json.loads(r[1]) for r in c.execute("SELECT id, display FROM tool_calls")}
        c.execute("PRAGMA user_version = 5")
        c.commit()
        c.close()
        assert shown["look"] == {"paths": ["a.png"], "blocks": [{
            "type": "files", "where": "under", "files": [{
                "id": "a1", "filename": "a.png", "kind": "image", "sha256": "s1", "bytes": 5,
                "media_type": "image/png", "width": 4, "height": 3}]}]}
        assert shown["shot"] == shot
        assert shown["send"] == {"attachments": [{"id": "a3"}]}
