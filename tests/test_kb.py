"""Chat archives: writing the same messages again, then search. Fixtures only, no real data."""
from __future__ import annotations

from types import SimpleNamespace

from celine.kb import KBs
from celine.kb import chat as chat_mod

from kb_fixtures import archive


def test_writing_again_is_idempotent_keeps_your_renames_and_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_mod, "LISTED_MIN_MESSAGES", 0)   # fixture people write little
    kbs = KBs(SimpleNamespace(data_dir=tmp_path, problems=[], kb={
        "whatsapp": {"kind": "chat", "title": "WhatsApp"}}), lambda *a: None)
    wa = kbs.get("whatsapp")
    assert archive(wa) == 10
    assert archive(wa) == 0
    bea = next(p for p in wa.people()["people"] if p["name"] == "+49 170 0000002")
    wa.update_person(bea["id"], "Bea")
    assert archive(wa, extra=True) == 1
    assert any(p["name"] == "Bea" for p in wa.people()["people"]), "a write does not undo it"
    # and what it brought is found, half-typed syntax and all
    assert kbs.search("celine")["total"] == 1
    assert kbs.search("cel")["total"] == 1                      # prefix while typing
    assert kbs.search('"awful last"')["total"] == 1
    assert kbs.search("foo OR")["total"] == 0


def test_kb_file_version_and_old_file_refused(tmp_path):
    import sqlite3
    import pytest
    from celine.kb.db import CHAT_MIGRATIONS, KBFile

    fresh = tmp_path / "fresh.db"
    KBFile(fresh, CHAT_MIGRATIONS)
    assert sqlite3.connect(fresh).execute("PRAGMA user_version").fetchone() == (4,)

    old = tmp_path / "old.db"
    sqlite3.connect(old).executescript("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);")
    with pytest.raises(RuntimeError, match=r"old\.db has no schema version"):
        KBFile(old, CHAT_MIGRATIONS)


def test_chat_0003_forgets_what_only_the_bridge_kept(tmp_path):
    """The bridge's cursor, its switch and the marker the user set lived in the archive's
    kv; they go, and nothing else there."""
    import shutil
    import sqlite3
    from celine.db.migrate import migrate
    from celine.kb.db import CHAT_MIGRATIONS
    d = tmp_path / "chat"
    d.mkdir()
    for f in sorted(CHAT_MIGRATIONS.glob("000[1-2]_*.sql")):
        shutil.copy(f, d)
    db = tmp_path / "wa.db"
    assert migrate(db, d) == 2
    c = sqlite3.connect(db)
    c.executemany("INSERT INTO kv(key, value) VALUES(?, '1')",
                  [("bridge_cursor",), ("bridge_since",), ("marker_own",), ("login_account",)])
    c.commit()
    c.close()
    shutil.copy(next(CHAT_MIGRATIONS.glob("0003_*.sql")), d)
    assert migrate(db, d) == 3
    assert sqlite3.connect(db).execute("SELECT key FROM kv").fetchall() == [("login_account",)]


def test_a_restored_file_is_reopened(tmp_path):
    import shutil
    import sqlite3
    from celine.kb.chat import ChatKB
    kb = ChatKB(id="w", title="w", platform="w", path=tmp_path / "w.db")
    assert kb.info()["messages"] == 0
    other = ChatKB(id="o", title="o", platform="o", path=tmp_path / "o.db")
    s = other.sink()
    s.message(s.chat("c", kind="group", name="G"), "1", ts=1, text="x")
    s.close()
    src, dst = sqlite3.connect(other.path), sqlite3.connect(tmp_path / "copy.db")
    src.backup(dst)
    src.close()
    dst.close()
    shutil.move(tmp_path / "copy.db", kb.path)          # a restore: new inode, same path
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"w.db{suffix}").unlink(missing_ok=True)
    assert kb.info()["messages"] == 1
