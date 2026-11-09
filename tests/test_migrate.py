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
    "guard_decisions", "agent", "text_versions", "compactions", "freezes", "guard_passes", "work",
    "tabs", "bugreports", "plans", "push_subscriptions", "model_grants",
}
LOG_TABLES = {"events", "global_events", "raw_responses", "requests", "terminal_commands"}
NOTEBOOK_TABLES = {"notebook_index", "notebook_fts"}
LATEST = 28


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
    with pytest.raises(m.Failed, match="this build knows"):
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
        with pytest.raises(m.Failed) as e:
            migrate(db)
    assert str(e.value) == (
        "x.db stays at schema version 2: 0003_bad.sql failed (no such table: nope). "
        f"{tmp_path / 'x.db.pre-3.bak'} is the file from before that update.")
    c = sqlite3.connect(tmp_path / "x.db.pre-3.bak")
    assert c.execute("SELECT k FROM kv").fetchall() == [("a",)]


def test_a_database_from_a_newer_release_is_refused_with_the_copy_to_go_back_to(
        tmp_path, monkeypatch):
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_init.sql").write_text("CREATE TABLE kv(k TEXT PRIMARY KEY);\n")
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "x.db"
    assert migrate(db) == 1
    (d / "0002_more.sql").write_text("CREATE TABLE more(k TEXT);\n")
    assert migrate(db) == 2
    (d / "0002_more.sql").unlink()
    with pytest.raises(m.Failed) as e:
        migrate(db)
    assert str(e.value) == (
        "x.db is at schema version 2 and this build knows 1. Start the release that "
        f"wrote it. {tmp_path / 'x.db.pre-2.bak'} is the file from before that update.")


def test_a_start_on_a_database_it_cannot_migrate_is_a_one_line_refusal(tmp_path, capsys):
    from celine import __main__ as main
    db = tmp_path / "celine.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version = 9999")
    c.close()
    with pytest.raises(SystemExit) as e:
        main.migrated(db)
    assert e.value.code == 2
    assert capsys.readouterr().err.startswith(
        "celine: celine.db is at schema version 9999 and this build knows ")


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
    assert got.pop("s-br") == "/chat/s-br", "a bridged chat's (a chat after 0005) opens with the agent's chats"
    assert got == {sub: homes.of(c, sub)["link"] for sub in subs}
    assert got["s-run"] == "/routines/2026-10-22/task/s-run" and got["gone"] is None
    assert "link" not in json.loads(c.execute(
        "SELECT display FROM tool_calls WHERE id='other'").fetchone()[0]), "only the agent's calls"
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


def test_0015_takes_keep_thinking_out_of_the_saved_models_once(tmp_path):
    import importlib.util
    import tomllib
    spec = importlib.util.spec_from_file_location(
        "m0015", MIGRATIONS_DIR / "0015_no_keep_thinking.py")
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    db = tmp_path / "celine.db"
    step.up(None, db)                                  # no settings: nothing to do
    assert not (tmp_path / "settings").exists()
    path = tmp_path / "settings" / "providers.toml"
    path.parent.mkdir()
    path.write_text('[providers.gpu]\nkind = "llama-server"\n\n'
                    '[providers.gpu.models.big]\ncontext = 32768\nkeep_thinking = 1\n\n'
                    '[providers.gpu.models.small]\ncontext = 8192\n')
    step.up(None, db)
    got = tomllib.loads(path.read_text())
    assert got["providers"]["gpu"]["models"] == {"big": {"context": 32768},
                                                 "small": {"context": 8192}}
    before = path.read_bytes()
    step.up(None, db)
    assert path.read_bytes() == before, "run again, it writes nothing"
    assert not list(path.parent.glob("*.tmp")), "written atomically, no temp file left"
    migrate_all(db)
    assert path.read_bytes() == before, "the runner applies it to a fresh database too"


def test_0025_makes_a_step_model_set_before_the_agents_once():
    import importlib.util
    import json
    spec = importlib.util.spec_from_file_location(
        "m0025", MIGRATIONS_DIR / "0025_model_grants.py")
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE plans (conversation_id TEXT PRIMARY KEY, steps TEXT)")
    c.execute("INSERT INTO plans VALUES('c', ?)", (json.dumps(
        [{"title": "Read", "model": None}, {"title": "Dig", "model": "or/glm"}]),))
    for _ in range(2):
        step.up(c, None)
        assert json.loads(c.execute("SELECT steps FROM plans").fetchone()[0]) == [
            {"title": "Read", "model": None, "by": None},
            {"title": "Dig", "model": "or/glm", "by": "her"}]
    assert c.execute("SELECT count(*) FROM model_grants").fetchone() == (0,)


def test_the_agents_stored_persona_gets_the_lines_the_shipped_one_has():
    import importlib.util

    from celine.agent import BASE, base_persona
    spec = importlib.util.spec_from_file_location(
        "m0017", MIGRATIONS_DIR / "0017_persona_lines.py")
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    shipped = (BASE / "persona.md").read_text()
    old = shipped.replace(step.ADDED, "").rstrip() + step.DUPLICATE + "\n\n" + \
        (BASE / "owner.md").read_text()
    assert step.changed(old) == base_persona()
    assert step.changed(base_persona()) == base_persona(), "run again, it writes nothing"


def test_the_agents_stored_persona_gets_the_training_line_once():
    import importlib.util

    from celine.agent import base_persona
    spec = importlib.util.spec_from_file_location(
        "m0019", MIGRATIONS_DIR / "0019_persona_training_line.py")
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    old = base_persona().replace(step.ADDED, "")
    assert step.changed(old) == base_persona()
    assert step.changed(base_persona()) == base_persona(), "run again, it writes nothing"
    assert step.changed("my own persona") == "my own persona"


def test_0026_renames_the_table_and_its_values_and_a_saved_she_becomes_its_name(
        tmp_path, monkeypatch):
    import json
    d = tmp_path / "migrations"
    d.mkdir()
    for f in sorted(MIGRATIONS_DIR.iterdir()):
        if f.suffix in (".sql", ".py") and f.name < "0026":
            shutil.copy(f, d)
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "celine.db"
    assert migrate_all(db) == 25
    c = sqlite3.connect(db)
    c.execute("INSERT INTO her(id, name, pronouns, color, persona, talk, updated_at) "
              "VALUES(1, 'Tink', 'they', 'rose', 'You are {{name}}. {{She}} hums.', NULL, 'x')")
    c.execute("INSERT INTO text_versions(of, text, by, change, size, created_at) "
              "VALUES('her:persona', 'Ask {{them}} about {{her}} day.', 'her', '', 1, 'x')")
    c.execute("INSERT INTO kv(k, v) VALUES('bastet:talk', 'You watch {{herself}}.')")
    c.execute("INSERT INTO moods(at, emotion, strength, by) VALUES('x', 'calm', 0.5, 'her')")
    c.execute("INSERT INTO runs(id, schedule, label, slot, night, state, created_at, "
              "started_by) VALUES('r1', 'diary', 'Diary', 's', 'n', 'done', 'x', 'her')")
    c.execute("INSERT INTO conversations(id, created_at, updated_at) VALUES('c', 'x', 'x')")
    c.execute("INSERT INTO plans(conversation_id, steps, updated_at) VALUES('c', ?, 'x')",
              (json.dumps([{"title": "Dig", "by": "her"}, {"title": "Read", "by": "user"}]),))
    c.commit()
    c.close()
    log = sqlite3.connect(log_path(db))
    log.execute("INSERT INTO global_events(type, data, created_at) VALUES('her.hold', "
                "'{\"hold\": null}', 'x')")
    log.commit()
    log.close()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "turns.title.md").write_text("Name {{her}} chat.")
    (tmp_path / "settings" / "schedules").mkdir(parents=True)
    (tmp_path / "settings" / "schedules" / "diary.toml").write_text(
        'prompt = "{{She}} writes."\n')
    shutil.copy(MIGRATIONS_DIR / "0026_agent_word.py", d)
    for _ in range(2):                                # run again, the same
        assert migrate_all(db) == 26
        c = sqlite3.connect(db)
        assert "her" not in _tables(db)
        assert c.execute("SELECT name, persona FROM agent").fetchone() == (
            "Tink", "You are {{name}}. {{name}} hums.")
        assert c.execute("SELECT of, text, by FROM text_versions").fetchone() == (
            "agent:persona", "Ask {{name}} about {{name}} day.", "agent")
        assert c.execute("SELECT v FROM kv WHERE k='bastet:talk'").fetchone() == (
            "You watch {{name}}.",)
        assert c.execute("SELECT by FROM moods").fetchone() == ("agent",)
        assert c.execute("SELECT started_by FROM runs").fetchone() == ("agent",)
        c.execute("UPDATE runs SET started_by='agent' WHERE id='r1'")   # the CHECK takes it
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE runs SET started_by='her' WHERE id='r1'")
        assert [s["by"] for s in json.loads(c.execute("SELECT steps FROM plans").fetchone()[0])] \
            == ["agent", "user"]
        c.close()
        assert sqlite3.connect(log_path(db)).execute(
            "SELECT type FROM global_events").fetchone() == ("agent.hold",)
        assert (tmp_path / "prompts" / "turns.title.md").read_text() == "Name {{name}} chat."
        assert (tmp_path / "settings" / "schedules" / "diary.toml").read_text() == \
            'prompt = "{{name}} writes."\n'


def test_0027_names_the_reviewer_keys_and_moves_its_prompt_file(tmp_path, monkeypatch):
    import importlib.util
    import tomllib
    d = tmp_path / "migrations"
    d.mkdir()
    for f in sorted(MIGRATIONS_DIR.iterdir()):
        if f.suffix in (".sql", ".py") and f.name < "0027":
            shutil.copy(f, d)
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "celine.db"
    assert migrate_all(db) == 26
    folder = tmp_path / "settings" / "schedules"
    folder.mkdir(parents=True)
    (folder / "talk.toml").write_text(
        '# kept\nlabel = "Talk"\npartner = "talk-partner.md"\nrounds = 3\nprompt = "Go."\n')
    (folder / "talk-partner.md").write_text("You review.\n")
    (folder / "mentor.toml").write_text(   # a prompt line that reads like a key
        'label = "M"\npartner = "mentor.md"\nprompt = """\nrounds = 9\n"""\n')
    (folder / "plain.toml").write_text('label = "P"\nprompt = "Go."\n')
    shutil.copy(MIGRATIONS_DIR / "0027_reviewer_keys.py", d)
    assert migrate_all(db) == 27
    talk = '# kept\nlabel = "Talk"\nreviewer = "talk-reviewer.md"\nturns = 3\nprompt = "Go."\n'
    assert (folder / "talk.toml").read_text() == talk
    assert (folder / "talk-reviewer.md").read_text() == "You review.\n"
    assert not (folder / "talk-partner.md").exists()
    assert tomllib.loads((folder / "mentor.toml").read_text()) == {
        "label": "M", "reviewer": "mentor.md", "prompt": "rounds = 9\n"}
    assert (folder / "plain.toml").read_text() == 'label = "P"\nprompt = "Go."\n'
    spec = importlib.util.spec_from_file_location("m0027", d / "0027_reviewer_keys.py")
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    before = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
    step.up(None, db)                                 # run again, it writes nothing
    assert {p.name: p.stat().st_mtime_ns for p in folder.iterdir()} == before


def test_0028_keeps_a_missions_check_in_time_and_reads_its_old_check_ins(tmp_path, monkeypatch):
    d = tmp_path / "migrations"
    d.mkdir()
    for f in sorted(MIGRATIONS_DIR.iterdir()):
        if f.suffix in (".sql", ".py") and f.name < "0028":
            shutil.copy(f, d)
    monkeypatch.setattr(m, "MIGRATIONS_DIR", d)
    db = tmp_path / "celine.db"
    assert migrate_all(db) == 27
    at = "2026-11-01T10:00:00.000+00:00"
    c = sqlite3.connect(db)
    c.execute("INSERT INTO conversations(id, created_at, updated_at, kind) "
              "VALUES('m1', ?, ?, 'mission')", (at, at))
    c.execute("INSERT INTO missions(conversation_id, name, terminal, status, created_at, "
              "updated_at) VALUES('m1', 'Prices', 'prices', 'needs_you', ?, ?)", (at, at))
    for iid, data in ((1, '{"check_in": true}'), (2, '{}')):   # a check-in, a question
        c.execute("INSERT INTO intercom(id, created_at, from_addr, to_addr, kind, text, data) "
                  "VALUES(?, ?, 'agent:m1', 'user', 'report', 'x', ?)", (iid, at, data))
        c.execute("INSERT INTO intercom_deliveries(intercom_id, to_addr, state) "
                  "VALUES(?, 'user', 'pending')", (iid,))
    c.commit()
    c.close()
    shutil.copy(MIGRATIONS_DIR / "0028_mission_check_ins.sql", d)
    assert migrate_all(db) == 28
    c = sqlite3.connect(db)
    assert c.execute("SELECT checked_in_at FROM missions").fetchone() == (at,)
    assert dict(c.execute("SELECT intercom_id, state FROM intercom_deliveries")) == {
        1: "read", 2: "pending"}, "a check-in stops waiting, a question still waits"
    c.close()
