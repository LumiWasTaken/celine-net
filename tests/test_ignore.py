"""Delete and ignore (kb/ignore.py) and `celine purge`: a deleted chat leaves nothing
behind and stays gone, and nothing else of its own goes with it. Fixture data only."""
from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from celine.kb import identity, instant, passages
from celine.kb.chat import ChatKB
from celine.kb.feed import Consumer
from celine.kb.connectors import Refused
from celine.kb.ignore import delete_and_ignore, include_again
from celine.kb.media import media_dir

from kb_fixtures import archive

T0 = 1_758_700_000_000
KAI = "4915550000001@s.whatsapp.net"
GROUP, OTHER = "12345-678@g.us", "999-111@g.us"
PNG_A, PNG_B = b"\x89PNG only in the group", b"\x89PNG in both chats"
SHA_A, SHA_B = hashlib.sha256(PNG_A).hexdigest(), hashlib.sha256(PNG_B).hexdigest()


def _q(kb: ChatKB, sql: str, *args):
    return kb.file.read_sync(lambda c: c.execute(sql, args).fetchall())


def _msg(seq, chat, key, ts, text, sha=None):
    m = {"key": key, "ts": ts, "from_me": False, "type": "text", "text": text,
         "sender": {"key": KAI, "push_name": "Kai"}}
    if sha:
        m |= {"type": "media", "media": {"type": "photo", "files": [{"sha256": sha}]}}
    return {"seq": seq, "kind": "message", "chat": {"key": chat, "kind": "group",
                                                    "name": chat[:3]}, "message": m}


@pytest.fixture
def wa(tmp_path) -> ChatKB:
    """The group: 6 messages, a file only it has, a file shared with the other (5)."""
    kb = ChatKB(id="whatsapp", title="WhatsApp", platform="whatsapp",
                path=tmp_path / "kb" / "whatsapp.db")
    Consumer(kb).apply(
        [_msg(i + 1, GROUP, f"G{i}", T0 + i * 60_000, f"garage talk {i}",
              {1: SHA_A, 2: SHA_B}.get(i)) for i in range(6)]
        + [_msg(10 + i, OTHER, f"O{i}", T0 + i * 60_000, f"other talk {i}",
                SHA_B if i == 4 else None) for i in range(5)]
        + [{"seq": 20, "kind": "edit", "chat_key": GROUP, "message_key": "G0",
            "ts": T0 + 999, "text": "garage talk, edited"}])
    media_dir(kb).mkdir(parents=True)
    (media_dir(kb) / SHA_A).write_bytes(PNG_A)
    (media_dir(kb) / SHA_B).write_bytes(PNG_B)
    passages.build(kb.path, platform="whatsapp", chats={}, sp=passages.SessionParams(),
                   pp=passages.PassageParams(), check=lambda: None, progress=lambda *a: None)
    kb.file.write_sync(lambda c: (instant.backfill(c), instant.drain(c)))
    return kb


def _chat_id(kb: ChatKB, key: str) -> int:
    return _q(kb, "SELECT id FROM chats WHERE key = ?", key)[0][0]


def test_a_deleted_chat_leaves_nothing_behind_and_nothing_else_goes(wa):
    gid, oid = _chat_id(wa, GROUP), _chat_id(wa, OTHER)
    with pytest.raises(Refused):                     # one unknown id: nothing is deleted
        delete_and_ignore(wa, [gid, 9999])
    assert wa.get_chat(gid)["messages"] == 6
    out = delete_and_ignore(wa, [gid])
    assert out["messages"] == 6
    for table in ("messages", "sessions", "passages", "search_map"):
        assert _q(wa, f"SELECT COUNT(*) FROM {table} WHERE chat_id = ?", gid)[0][0] == 0, table
    assert _q(wa, "SELECT COUNT(*) FROM message_edits")[0][0] == 0
    assert _q(wa, "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'garage'")[0][0] == 0
    # the file only the group had goes; the shared one stays with the other chat, untouched
    assert not (media_dir(wa) / SHA_A).exists() and (media_dir(wa) / SHA_B).exists()
    assert wa.get_chat(oid)["messages"] == 5
    assert _q(wa, "SELECT COUNT(*) FROM search_map WHERE chat_id = ?", oid)[0][0] == 5
    assert [c["key"] for c in wa.ignored_chats()] == [GROUP]


def test_an_ignored_chat_stays_gone_whoever_writes(wa, tmp_path):
    delete_and_ignore(wa, [_chat_id(wa, GROUP)])
    got = Consumer(wa).apply([_msg(30, GROUP, "G7", T0 + 10 * 60_000, "still talking"),
                              _msg(31, OTHER, "O9", T0 + 10 * 60_000, "the other goes on")])
    assert got.skipped == 1 and got.added == 1
    # any other writer too: the fixture archive has a group with this key
    archive(wa)
    assert not _q(wa, "SELECT 1 FROM chats WHERE key = ?", GROUP)
    # included again, the next message lands
    assert wa.unignore([GROUP]) == [GROUP]
    Consumer(wa).apply([_msg(35, GROUP, "G8", T0 + 11 * 60_000, "back")])
    assert _q(wa, "SELECT message_count FROM chats WHERE key = ?", GROUP)[0][0] == 1


class FakeLink:
    """The WhatsApp connection's doors, as far as delete and ignore uses them."""

    def __init__(self):
        self.calls = []

    def admin_sync(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"write_chat": "4917000000000@s.whatsapp.net"} if path == "/admin/info" else {}


def test_whatsapp_deletes_the_chat_from_its_journal_too(wa):
    link = FakeLink()
    delete_and_ignore(wa, [_chat_id(wa, GROUP)], link)
    assert ("POST", "/admin/ignored", {"chats": [GROUP]}) in link.calls
    assert [c["key"] for c in wa.ignored_chats()] == [GROUP]
    include_again(wa, GROUP, link)
    assert link.calls[-1] == ("DELETE", "/admin/ignored?chat=12345-678%40g.us", None)
    assert wa.ignored_chats() == []


def test_purge_deletes_small_chats_and_unused_people_and_keeps_the_rest(tmp_path):
    from celine import ops
    from celine.db.migrate import migrate
    kb = ChatKB(id="whatsapp", title="WhatsApp", platform="whatsapp",
                path=tmp_path / "kb" / "whatsapp.db")
    s = kb.sink()
    ids = {n: s.person(f"{n}@s.whatsapp.net", push_name=n)
           for n in ("five", "four", "linked", "named", "peer", "nobody", "gone")}
    grp, tiny = s.chat("g@g.us", kind="group", name="Group"), s.chat("t@g.us", kind="group")
    dm = s.chat("peer@s.whatsapp.net", person_key="peer@s.whatsapp.net")
    for i, who in enumerate(["five"] * 5 + ["four"] * 4 + ["linked"]):
        s.message(grp, f"m{i}", ts=T0 + i, text="hi", sender=ids[who])
    s.message(dm, "d1", ts=T0, text="only mine", from_me=True)
    s.message(tiny, "t1", ts=T0, text="bye", sender=ids["gone"])
    s.close()
    kb.update_person(ids["named"], "My name for them")
    db = tmp_path / "celine.db"
    migrate(db)
    with sqlite3.connect(db) as c:
        identity.link_sync(c, [f"whatsapp:{ids['linked']}", "discord:1"], name="L")
    cfg = SimpleNamespace(data_dir=tmp_path, problems=[], mcp={}, db_path=db, kb={
        "whatsapp": {"kind": "chat", "title": "WhatsApp", "platform": "whatsapp"}})

    count = "SELECT (SELECT COUNT(*) FROM chats), (SELECT COUNT(*) FROM people)"
    before = tuple(_q(kb, count)[0])
    assert ops.purge(cfg, 5, dry_run=True) == 0
    assert tuple(_q(kb, count)[0]) == before, "a dry run changes nothing"
    assert ops.purge(cfg, 5, dry_run=False) == 0
    assert {c["key"] for c in kb.ignored_chats()} == {"t@g.us", "peer@s.whatsapp.net"}
    left = {r[0] for r in _q(kb, "SELECT id FROM people")}
    # people with messages, the one it named, the linked one: all stay
    assert {ids["five"], ids["four"], ids["linked"], ids["named"]} <= left
    assert not {ids["nobody"], ids["gone"], ids["peer"]} & left
