"""Celine's retrieval: a trashed chat stays out."""
from __future__ import annotations

from celine.kb.retrieve import resolve, retrieve

from kb_fixtures import CTX, H, T23, archive_skill, build, embed_cfg, fake_embed


def test_a_trashed_chat_stays_out_of_meaning_search_too(tmp_path, monkeypatch):
    """The word legs left trashed chats out of an "all" scope; the meaning leg and the
    passage words brought them back."""
    from celine.kb import semantic
    kbs = build(tmp_path)
    wa = kbs.get("whatsapp")
    monkeypatch.setattr(semantic, "embed_sync", fake_embed)
    semantic.build(embed_cfg(), wa, CTX)
    chat, text = wa.file.read_sync(lambda c: c.execute(
        "SELECT chat_id, text FROM passages WHERE text LIKE '%mast%'").fetchone())

    def embed(texts):
        return [fake_embed(None, [text])[0] for _ in texts]
    embed.fp = None                             # type: ignore[attr-defined]
    found = retrieve(kbs, ["zzzq"], resolve(kbs, kb_ids="whatsapp"), about=[text], embed=embed)
    assert chat in {e.chat_id for e in found.episodes}
    wa.file.write_sync(lambda c: c.execute("UPDATE chats SET trashed = 1 WHERE id = ?", (chat,)))
    res = retrieve(kbs, ["zzzq"], resolve(kbs, kb_ids="whatsapp"), about=[text], embed=embed)
    assert chat not in {e.chat_id for e in res.episodes}


def test_a_slow_reranker_costs_a_search_one_budget_then_it_is_ranked_by_fusion(
        tmp_path, monkeypatch):
    """Celine's note: the reranker timing out failed whole searches (each of its three
    queries waited the full timeout, past the tool's own limit)."""
    from types import SimpleNamespace

    import httpx
    from celine.kb.retrieve import rerank
    waited = []
    clock = [100.0]

    def slow(url, json, headers, timeout):
        waited.append(timeout.read)
        clock[0] += 0.15
        return httpx.Response(200, json={"results": []})
    monkeypatch.setattr(rerank.httpx, "post", slow)
    monkeypatch.setattr(rerank, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"),
                   about=["the celine floats", "a new mast", "which sails"],
                   reranker=rerank.HttpReranker("http://r/rerank", "m", timeout=0.2))
    assert len(waited) == 2 and waited[1] < 0.1, "the second call gets what is left"
    assert not res.reranked and res.episodes
    assert any(n.startswith("Reranker unavailable") for n in res.notes)


def test_the_exact_total_counts_what_the_hits_match(tmp_path):
    """The total said 0 next to episodes that matched: it counted on the plain word
    index, which does not fold "ü" to "ue"."""
    kbs = build(tmp_path)
    s = kbs.get("whatsapp").sink()
    grp = s.chat("123-456@g.us", kind="group")
    for i in range(3):
        s.message(grp, f"m{i}", ts=T23 + (i + 1) * H, text="Müller kommt gleich",
                  from_me=True)
    s.close()
    res = retrieve(kbs, ["mueller"], resolve(kbs, kb_ids="whatsapp"), mode="all")
    assert res.totals == {"mueller": 3} and sum(res.years.values()) == 3


def test_a_whatsapp_group_reads_by_its_name_never_its_id(tmp_path):
    from celine.kb.chat import chat_cards
    kbs = build(tmp_path)
    wa = kbs.get("whatsapp")
    gid = wa.file.read_sync(lambda c: c.execute(
        "SELECT id FROM chats WHERE key = '123-456@g.us'").fetchone()[0])
    assert wa.file.read_sync(lambda c: chat_cards(c, {gid}))[gid]["name"] == "Chess club"
    for stored in (None, "123-456@g.us"):          # no subject in the archive
        wa.file.write_sync(lambda c, s=stored: c.execute(
            "UPDATE chats SET name = ? WHERE id = ?", (s, gid)))
        assert wa.file.read_sync(lambda c: chat_cards(c, {gid}))[gid]["name"] == "group 3456"
        assert wa.get_chat(gid)["name"] == "group 3456"


def test_look_up_lists_the_chats_a_person_writes_in(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import anyio
    skill = archive_skill(monkeypatch, build(tmp_path))
    out = anyio.run(lambda: skill.look_up(SimpleNamespace(turn_id=None, store=None),
                                          name="Bea")).content
    assert "their chats, most messages first:" in out
    assert "chat whatsapp:2 Chess club (group), 3 by them" in out
    assert 'written_by=["whatsapp:3"]' in out
