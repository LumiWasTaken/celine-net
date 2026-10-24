"""The passage index: incremental builds that resume, an index never reset by a model it
cannot reach, and passage search. Fake embedder, tiny synthetic chats, no network."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from celine.kb import embed, passages, semantic
from celine.kb.chat import ChatKB
from celine.kb.importers import Cancelled, ImportContext

from kb_fixtures import CTX, T0, embed_cfg, fake_embed

MIN = 60_000


def chat_kb(tmp_path):
    return ChatKB(id="whatsapp", title="WhatsApp", platform="whatsapp", path=tmp_path / "w.db")


def fill(kb, chat_key, msgs, name="Chess club"):
    """msgs: (key, ts, sender or None for me, text)."""
    s = kb.sink()
    cid = s.chat(chat_key, kind="group", name=name)
    people = {}
    for key, ts, who, text in msgs:
        if who and who not in people:
            people[who] = s.person(f"{who}@x", push_name=who.title())
        s.message(cid, key, ts=ts, text=text, sender=people.get(who), from_me=who is None)
    s.close()
    return cid


def q(kb, sql, *a):
    return kb.file.read_sync(lambda c: c.execute(sql, a).fetchall())


def _conversation(n, start=T0):
    return [(f"k{i}", start + i * MIN, None if i % 2 else "alex", f"message number {i} about cars")
            for i in range(n)]


def test_build_is_incremental_and_every_message_is_mapped(tmp_path, monkeypatch):
    texts = []

    def counting(settings, batch):
        if batch != embed.PROBES:                   # the fingerprint request is not counted
            texts.extend(batch)
        return fake_embed(settings, batch)
    monkeypatch.setattr(semantic, "embed_sync", counting)
    kb = chat_kb(tmp_path)
    fill(kb, "g1", _conversation(40))
    first = semantic.build(embed_cfg(), kb, CTX)
    assert first["passages"] >= 3 and first["embedded"] == first["passages"]
    assert semantic.build(embed_cfg(), kb, CTX)["embedded"] == 0
    assert q(kb, "SELECT COUNT(DISTINCT message_id) FROM passage_members")[0][0] == 40
    texts.clear()
    fill(kb, "g1", [("late", T0 + 40 * MIN, "alex", "one more about cars")])
    assert semantic.build(embed_cfg(), kb, CTX)["embedded"] == 1 and len(texts) == 1
    assert q(kb, "SELECT COUNT(*) FROM passages WHERE embedded = 0")[0][0] == 0
    assert kb.file.read_sync(passages.status)["ready"] is True


def test_a_new_way_of_writing_passages_cuts_every_chat_again_once(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "embed_sync", fake_embed)
    kb = chat_kb(tmp_path)
    fill(kb, "g1", _conversation(20))
    semantic.build(embed_cfg(), kb, CTX)
    head = q(kb, "SELECT header FROM passages LIMIT 1")[0][0].split("\n")
    assert head[:2] == ['WhatsApp group "Chess club"', "Alex, me"] and len(head) == 3
    assert not kb.file.read_sync(semantic.due)
    monkeypatch.setattr(passages, "RENDER_VER", passages.RENDER_VER + 1)
    assert kb.file.read_sync(semantic.due)
    assert semantic.build(embed_cfg(), kb, CTX)["chats"] == 1
    assert not kb.file.read_sync(semantic.due)


def test_a_cancelled_build_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "embed_sync", fake_embed)
    kb = chat_kb(tmp_path)
    fill(kb, "g1", _conversation(60))
    calls = {"n": 0}

    def cancel_soon():
        calls["n"] += 1
        return calls["n"] > 3
    with pytest.raises(Cancelled):
        semantic.build(embed_cfg(workers=1, batch=1), kb,
                       ImportContext(progress=lambda *a: None, cancelled=cancel_soon))
    left = q(kb, "SELECT COUNT(*) FROM passages WHERE embedded = 0")[0][0]
    assert left > 0 and semantic.build(embed_cfg(), kb, CTX)["embedded"] == left
    assert q(kb, "SELECT COUNT(*) FROM passages WHERE embedded = 0")[0][0] == 0


def test_search_finds_the_passage_once_it_is_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "embed_sync", fake_embed)
    kb = chat_kb(tmp_path)
    fill(kb, "g1", [(f"a{i}", T0 + i * MIN, "alex", "chess server lagging tonight")
                    for i in range(5)])
    b = fill(kb, "g2", [(f"b{i}", T0 + 90 * 24 * 60 * MIN + i * MIN, "bea",
                         "celine engine repair garage") for i in range(5)], name="Garage")
    kbs = SimpleNamespace(pick=lambda ids: [kb], get=lambda i: kb if i == kb.id else None)
    assert semantic.search_sync(embed_cfg(), kbs, "celine engine garage")["hits"] == []
    semantic.build(embed_cfg(), kb, CTX)
    top = semantic.search_sync(embed_cfg(), kbs, "celine engine garage", limit=5)["hits"][0]
    assert top["chat_id"] == b and "celine" in top["text"]
    assert top["ref"] == f"whatsapp:{top['messages'][0]}"
