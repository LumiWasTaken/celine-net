"""Celine's retrieval: a trashed chat stays out."""
from __future__ import annotations

from celine.kb.retrieve import resolve, retrieve

from kb_fixtures import (
    CTX,
    H,
    T21,
    T23,
    WordReranker,
    archive_skill,
    build,
    embed_cfg,
    fake_embed,
)


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


def _ctx():
    from types import SimpleNamespace
    return SimpleNamespace(turn_id=None, store=None, max_output_tokens=None, emit=None,
                           conversation_id="c")


def test_look_up_gives_a_person_with_their_names_and_chats(tmp_path, monkeypatch):
    import anyio
    skill = archive_skill(monkeypatch, build(tmp_path))
    out = anyio.run(lambda: skill.look_up(_ctx(), name="Bea")).content
    assert "person whatsapp:person:3 Bea" in out
    assert "other chats they write in, most messages first:" in out
    assert "chat whatsapp:chat:2 Chess club (group), 3 by them" in out


def test_a_name_where_a_reference_belongs_is_refused_with_who_it_could_be(tmp_path,
                                                                           monkeypatch):
    import anyio
    skill = archive_skill(monkeypatch, build(tmp_path))
    out = anyio.run(lambda: skill.count(_ctx(), by="person", written_by=["Bea"]))
    assert out.is_error and "written_by takes person references, not names" in out.content
    assert "person whatsapp:person:3 Bea" in out.content, "who the name could be"
    out = anyio.run(lambda: skill.count(_ctx(), by="person", chats=["Chess club"]))
    assert out.is_error and "chat whatsapp:chat:2 Chess club" in out.content
    out = anyio.run(lambda: skill.count(_ctx(), by="person", chats=["whatsapp:chat:2"],
                                        written_by=["whatsapp:person:3"]))
    assert not out.is_error and "Bea" in out.content and "Chess club" in out.content


def test_a_reference_of_the_wrong_kind_is_refused_where_it_is_passed(tmp_path, monkeypatch):
    """A person's reference is no chat, whatever its number: the number 3 is also a chat."""
    import anyio
    skill = archive_skill(monkeypatch, build(tmp_path))
    out = anyio.run(lambda: skill.search(_ctx(), about=["chess"],
                                         chats=["whatsapp:person:3"]))
    assert out.is_error and "whatsapp:person:3 is a person, not a chat" in out.content
    out = anyio.run(lambda: skill.search(_ctx(), about=["chess"],
                                         written_by=["whatsapp:chat:2"]))
    assert out.is_error and "whatsapp:chat:2 is a chat, not a person" in out.content
    for old in (["whatsapp:2"], ["whatsapp:msg:2"]):
        out = anyio.run(lambda: skill.search(_ctx(), about=["chess"], chats=old))
        assert out.is_error, old
    out = anyio.run(lambda: skill.read_period(_ctx(), chat="whatsapp:person:3"))
    assert out.is_error and "not a chat" in out.content
    out = anyio.run(lambda: skill.read_session(_ctx(), ref="whatsapp:chat:2"))
    assert out.is_error and "whatsapp:chat:2 is a chat, not a message" in out.content


def test_scope_takes_all_or_source_ids(tmp_path):
    from celine.skills.archive import check_scope
    kbs = build(tmp_path)
    assert check_scope(kbs, None) == (None, None)
    assert check_scope(kbs, ["all"]) == (None, None)
    assert check_scope(kbs, ["whatsapp"]) == (["whatsapp"], None)
    assert check_scope(kbs, ["discord", "whatsapp"]) == (["discord", "whatsapp"], None)
    for bad in (["WhatsApp"], ["Chess club"], ["all", "discord"]):
        sources, why = check_scope(kbs, bad)
        assert sources is None and '"whatsapp"' in why and '"discord"' in why, bad
    assert "pass it in `chats`" in check_scope(kbs, ["whatsapp:chat:2"])[1]


def test_me_is_an_exact_word_in_written_by(tmp_path):
    from celine.kb.retrieve.scope import resolve
    kbs = build(tmp_path)
    assert resolve(kbs, people=["me"]).me
    try:
        got = resolve(kbs, people=["ME"])
    except ValueError:
        return
    assert not got.me


def test_a_reranker_error_answer_says_its_words(monkeypatch):
    import httpx
    import pytest
    from celine.kb.retrieve import rerank
    monkeypatch.setattr(rerank.httpx, "post", lambda url, json, headers, timeout: httpx.Response(
        400, json={"error": {"message": "the model is not a reranker"}}))
    with pytest.raises(rerank.RerankError, match="^the reranker said 400: the model is not a reranker$"):
        rerank.HttpReranker("http://r/rerank", "m").score("q", ["a doc"])


def test_a_reranker_counts_in_its_providers_hour(tmp_path, monkeypatch):
    """Requests per hour 1 on the reranker's provider: the second call fails with the
    sentence before the server is asked."""
    import httpx
    import pytest
    from celine.boot import load
    from celine.kb.retrieve import rerank
    from settings_fixtures import scratch
    scratch(tmp_path, monkeypatch, roles={"reranker": {"chain": ["rr/rank"]}}, providers={
        "rr": {"kind": "llama-swap", "base_url": "http://rr:8080/v1", "label": "Ranker",
               "options": {"max_per_hour": 1}, "models": {"rank": {"caps": ["rerank"]}}}})
    asked = []
    monkeypatch.setattr(rerank.httpx, "post", lambda url, json, headers, timeout: (
        asked.append(url), httpx.Response(200, json={"results": [{"index": 0,
                                                                   "relevance_score": 1}]}))[1])
    from celine.backends.router import app_router
    cfg = load()
    app_router(cfg).now = lambda: 100.0
    assert rerank.from_config(cfg).score("q", ["a doc"]) == [1.0]
    with pytest.raises(rerank.RerankError, match="^Ranker reached its 1 request this hour.$"):
        rerank.from_config(cfg).score("q", ["a doc"])
    assert asked == ["http://rr:8080/v1/rerank"]


# ---- absent literal words -----------------------------------------------------------


def test_absent_words_only_returns_nothing_without_embedding(tmp_path):
    """Every literal variant absent: the cheap exact-total preflight answers before any
    query embedding or dense retrieval."""
    kbs = build(tmp_path)
    called = []

    def embed(texts):
        called.append(texts)
        return fake_embed(None, texts)
    embed.fp = "fake"                             # type: ignore[attr-defined]

    res = retrieve(kbs, ["zzzqnosuch"], resolve(kbs), embed=embed)
    assert called == [], "no query embedding for words that match nothing in scope"
    assert res.episodes == []
    assert res.absent == ["zzzqnosuch"]
    assert res.totals == {"zzzqnosuch": 0}
    assert any("No indexed message in scope matches these exact words/phrases" in n
               for n in res.notes)


def test_a_single_word_still_matches_as_a_prefix(tmp_path):
    """The preflight counts exactly what the hits match: one word is a prefix."""
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celin"], resolve(kbs, kb_ids="whatsapp"))
    assert res.totals == {"celin": 3}
    assert res.absent == []


def test_about_only_leaves_absent_empty(tmp_path):
    """An about-only search has no literal variants to be absent."""
    kbs = build(tmp_path)
    res = retrieve(kbs, [], resolve(kbs, kb_ids="whatsapp"), about=["the celine floats"],
                   reranker=WordReranker())
    assert res.absent == []
    assert not any("exact words/phrases" in n for n in res.notes)


def test_a_partially_absent_word_list_keeps_the_present_words(tmp_path):
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine", "zzzqnosuch"], resolve(kbs, kb_ids="whatsapp"))
    assert res.episodes
    assert res.totals["celine"] == 3 and res.totals["zzzqnosuch"] == 0
    assert res.absent == ["zzzqnosuch"]
    assert any("zzzqnosuch" in n for n in res.notes)


def test_mixed_words_and_about_keeps_semantic_windows_with_a_note(tmp_path):
    """A mixed search is never suppressed because one literal name is absent: the windows
    stay, with the absent entry named and the semantic nature of the windows stated."""
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine", "zzzqnosuch"], resolve(kbs, kb_ids="whatsapp"),
                   about=["the boat which floats"], reranker=WordReranker())
    assert res.episodes
    assert res.absent == ["zzzqnosuch"]
    assert any("zzzqnosuch" in n and "semantic topic matches" in n for n in res.notes)


# ---- chronological literal guarantee ------------------------------------------------


def test_order_first_returns_the_earliest_literal_hit(tmp_path):
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"), order="first",
                   reranker=WordReranker())
    assert res.episodes
    assert res.episodes[0].start_ts == T21


def test_order_last_returns_the_latest_literal_hit(tmp_path):
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"), order="last",
                   reranker=WordReranker())
    assert res.episodes
    assert res.episodes[0].end_ts == T23 + 5 * H


def test_order_first_with_terse_about(tmp_path):
    kbs = build(tmp_path)
    res = retrieve(kbs, [], resolve(kbs, kb_ids="whatsapp"), about=["celine"],
                   order="first", reranker=WordReranker())
    assert res.episodes
    assert res.episodes[0].start_ts == T21


def test_order_last_with_terse_about(tmp_path):
    kbs = build(tmp_path)
    res = retrieve(kbs, [], resolve(kbs, kb_ids="whatsapp"), about=["celine"],
                   order="last", reranker=WordReranker())
    assert res.episodes
    assert res.episodes[0].end_ts == T23 + 5 * H


def test_descriptive_mixed_query_keeps_the_earliest_literal_hit(tmp_path):
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"),
                   about=["the boat that floats"], order="first", reranker=WordReranker())
    assert res.episodes
    assert res.episodes[0].start_ts == T21
    assert res.absent == []


def test_a_below_floor_rerank_keeps_the_earliest_literal_hit(tmp_path):
    """Even when every reranker score is below the floor, the literal word's chronological
    candidate survives: the earliest scoped occurrence is never dropped."""
    kbs = build(tmp_path)
    s = kbs.get("whatsapp").sink()
    grp = s.chat("900@g.us", kind="group", name="Fleet")
    for i in range(6):
        s.message(grp, f"x{i}", ts=T23 + (i + 10) * H, text=f"celine note {i}")
    s.close()

    class LowEarliest:
        def score(self, query, docs):
            return [-100.0 if "finally afloat" in d else -5.0 for d in docs]

    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"), order="first",
                   reranker=LowEarliest())
    assert res.reranked
    assert res.episodes[0].start_ts == T21
    assert not any("Nothing is clearly about it" in n for n in res.notes)


def test_a_below_floor_rerank_keeps_the_latest_literal_hit(tmp_path):
    kbs = build(tmp_path)
    s = kbs.get("whatsapp").sink()
    grp = s.chat("900@g.us", kind="group", name="Fleet")
    for i in range(6):
        s.message(grp, f"x{i}", ts=T23 + (i + 10) * H, text=f"celine note {i}")
    s.close()

    class LowLatest:
        def score(self, query, docs):
            return [-100.0 if "celine note 5" in d else -5.0 for d in docs]

    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"), order="last",
                   reranker=LowLatest())
    assert res.reranked
    assert res.episodes[0].end_ts == T23 + 15 * H


def test_order_best_is_unchanged_by_the_chrono_reservation(tmp_path):
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"), order="best",
                   reranker=WordReranker())
    assert res.episodes and res.reranked
    assert res.absent == []
    assert not any("Nothing is clearly about it" in n for n in res.notes)


# ---- reranker-unset note ------------------------------------------------------------


def test_no_reranker_says_fusion_supplied_the_ranking(tmp_path):
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"))
    assert res.episodes and not res.reranked
    assert any("fusion supplied the ranking" in n and "abstention" in n for n in res.notes)
    assert not any(n.startswith("Reranker unavailable") for n in res.notes)


# ---- the reranker's score scale ----------------------------------------------------


class Relevance:
    """A reranker that answers 0..1 relevance scores, as a relevance model declares."""

    scale = "relevance"

    def score(self, query, docs):
        return [0.9 if "floats" in d else 0.05 for d in docs]


def test_a_relevance_reranker_with_a_below_zero_floor_says_it_filters_nothing(tmp_path):
    from celine.kb.retrieve import Settings
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"),
                   about=["the boat which floats"], reranker=Relevance(),
                   settings=Settings(rerank_floor=-4.0))
    assert res.reranked
    assert any("scores 0 to 1" in n and "below 0" in n for n in res.notes)


def test_a_relevance_reranker_filters_at_a_relevance_floor(tmp_path):
    from celine.kb.retrieve import Settings
    kbs = build(tmp_path)
    res = retrieve(kbs, ["celine"], resolve(kbs, kb_ids="whatsapp"),
                   about=["the boat which floats"], reranker=Relevance(),
                   settings=Settings(rerank_floor=0.3))
    assert res.reranked and res.episodes
    assert not any("scores 0 to 1" in n for n in res.notes), "floor in range: no warning"


def test_a_reranker_can_declare_its_score_scale():
    import pytest
    from celine.settings.files import Invalid
    from celine.settings.registry import Settings, backend_config, validate_provider
    p = validate_provider("rr", {"kind": "llama-server", "base_url": "http://rr:8080/v1",
                                 "models": {"rank": {"caps": ["rerank"],
                                                     "scale": "relevance"}}})
    assert p["models"]["rank"]["scale"] == "relevance"
    assert backend_config(Settings(providers={"rr": p}), "rr/rank").raw["scale"] == \
        "relevance"
    with pytest.raises(Invalid, match="one of 'logit', 'relevance'"):
        validate_provider("rr", {"kind": "llama-server", "base_url": "http://rr:8080/v1",
                                 "models": {"rank": {"caps": ["rerank"], "scale": "cosine"}}})
    with pytest.raises(Invalid, match="Reranking model option"):
        validate_provider("g", {"kind": "llama-server", "base_url": "http://g:1/v1",
                                "models": {"m": {"caps": ["chat"], "scale": "logit"}}})
