"""Instant search: normalised matches, the index following edits and deletes, paging
without loss across knowledge bases, operators. Tiny fixtures only, no real data."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from celine.kb import KBs
from celine.kb.chat import ChatKB
from celine.kb.normalize import highlight, tokens

from chat_fixtures import until_sync
from kb_fixtures import T0


def _kbs(tmp_path: Path, texts: list[str]) -> KBs:
    """One chat archive "w" with `texts`, indexed before KBs warms the typo vocabulary."""
    kb = ChatKB(id="w", title="w", platform="w", path=tmp_path / "kb" / "w.db")
    _fill(kb, texts)
    kb.ix_catch_up()
    return KBs(SimpleNamespace(data_dir=tmp_path, problems=[],
                               kb={"w": {"kind": "chat", "title": "w"}}), lambda *a: None)


def _fill(kb: ChatKB, texts: list[str], *, sender: str = "p:bea", t0: int = T0,
          prefix: str = "k") -> None:
    s = kb.sink()
    pid = s.person(sender, display_name=sender.split(":")[1].title())
    cid = s.chat("c", kind="group", name="Chat")
    for i, t in enumerate(texts):
        s.message(cid, f"{prefix}{i}", ts=t0 + i * 60_000, text=t, sender=pid)
    s.close()


def test_normalised_matches_both_ways_and_snippets_are_escaped(tmp_path):
    kbs = _kbs(tmp_path, ["Die Straße ist gesperrt", "Herr Mueller kommt", "jaaaaa genau",
                          "alles gute zum geburtstag🎂", "Café au lait"])
    for q in ("strasse", "straße", "müller", "muller", "ja", "jaaaaaaa", "🎂", "cafe"):
        assert kbs.search(q)["total"] == 1, q
    assert "<mark>Mueller</mark>" in kbs.search("mueller")["hits"][0]["snippet"]
    assert highlight("Die Straße <b>", set(tokens("strasse"))) == \
        "Die <mark>Straße</mark> &lt;b&gt;"
    for q in ["*", "foo OR", "NEAR(", "(", '""', "-", "from:", "a:b:c", '"']:
        assert isinstance(kbs.search(q)["hits"], list), q


def test_the_index_follows_new_edited_and_deleted_rows_and_keeps_history(tmp_path):
    kbs = _kbs(tmp_path, ["erste nachricht", "dann"])
    kb = kbs.get("w")
    _fill(kb, ["zweite nachricht"], prefix="n", t0=T0 + 86_400_000)
    # a search never waits for the write lock: a busy drain goes to the worker thread
    until_sync(lambda: kbs.search("zweite")["total"] == 1)

    def edit(conn):
        conn.execute("UPDATE messages SET text = 'geänderte nachricht', edited = 1 "
                     "WHERE key = 'n0'")
        mid = conn.execute("SELECT id FROM messages WHERE key = 'k1'").fetchone()[0]
        conn.execute("INSERT INTO message_edits (message_id, ts, old_text) "
                     "VALUES (?, 5, 'vorher')", (mid,))
    kb.file.write_sync(edit)
    until_sync(lambda: kbs.search("zweite")["total"] == 0
               and kbs.search("geaenderte")["total"] == 1)
    kb.file.write_sync(lambda c: c.execute("UPDATE messages SET deleted_at = 99 "
                                           "WHERE key = 'k0'"))
    until_sync(lambda: [h["deleted"] for h in kbs.search("is:deleted")["hits"]] == [True])
    chat = kb.list_chats()["chats"][0]["id"]
    msgs = {m["text"]: m for m in kb.messages(chat)["messages"]}
    assert msgs["erste nachricht"]["deleted_at"] == 99, "a deleted message stays, marked"
    assert msgs["dann"]["edits"] == [{"ts": 5, "text": "vorher"}], "the old text stays"
    kb.file.write_sync(lambda c: c.execute("DELETE FROM messages WHERE key = 'k0'"))
    until_sync(lambda: kbs.search("erste")["total"] == 0)


def test_paging_across_knowledge_bases_loses_nothing(tmp_path):
    from celine.settings import general
    kbs = KBs(SimpleNamespace(data_dir=tmp_path, problems=[], kb={
        "whatsapp": {"kind": "chat", "title": "WhatsApp"},
        "discord": {"kind": "chat", "title": "Discord"},
        "documents": {"kind": "documents", "title": "Documents", "chunk_over_kb": 0}},
        r=lambda key: general.value({}, key)), lambda *a: None)
    _fill(kbs.get("whatsapp"), [f"pizza {i}" for i in range(10)], sender="p:alex")
    _fill(kbs.get("discord"), [f"pizza {i}" for i in range(10)], t0=T0 + 30_000)
    kbs.get("documents").write("food.md", "pizza recipe")
    seen, cursor = [], None
    for _ in range(10):
        r = kbs.search("pizza", limit=4, cursor=cursor)
        seen += [h["ref"] for h in r["hits"]]
        if not (cursor := r["next_cursor"]):
            break
    assert len(seen) == len(set(seen)) == 20, "messages only, each once"
    assert kbs.search("pizza kb:discord")["total"] == 10
    assert kbs.search("pizza from:alex")["total"] == 10


def test_paging_a_corrected_search_moves_on(tmp_path):
    kbs = _kbs(tmp_path, ["pancake night i"] * 12)
    for sort in ("recent", "best"):
        first = kbs.search("pancaek", limit=5, sort=sort)
        assert first.get("corrected") and len(first["hits"]) == 5, sort
        second = kbs.search("pancaek", limit=5, sort=sort, cursor=first["next_cursor"])
        a, b = {h["ref"] for h in first["hits"]}, {h["ref"] for h in second["hits"]}
        assert len(b) == 5 and not a & b, sort


def test_an_empty_typo_vocabulary_reloads_when_the_index_moves():
    import sqlite3
    from celine.kb.spell import VocabCache
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE search_vocab (term TEXT, col TEXT, doc INT)")
    cache = VocabCache()
    assert len(cache.get(conn, 1)) == 0
    conn.execute("INSERT INTO search_vocab VALUES ('hallo', 'body', 3)")
    assert len(cache.get(conn, 2)) == 1


def test_a_trashed_chat_is_out_of_search_even_when_named(tmp_path):
    kbs = _kbs(tmp_path, ["eine nachricht"])
    kb = kbs.get("w")
    chat = kb.list_chats()["chats"][0]["id"]
    kb.update_chat(chat, trashed=True)
    assert kb.list_chats()["chats"] == [] and kb.list_chats(trashed=True)["chats"][0]["trashed"]
    assert kbs.search("nachricht")["total"] == 0
