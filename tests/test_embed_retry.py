"""The embedding client: one try, and the server's reason when it fails."""
from __future__ import annotations

import httpx

from celine.kb import embed


def test_a_too_long_input_fails_with_the_servers_reason(monkeypatch):
    import pytest
    sizes = []

    def post(url, json, **kw):
        sizes.append(max(len(t) for t in json["input"]))
        return httpx.Response(400, json={"error": {
            "message": "input (3000 tokens) exceeds the available context size"}})

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(embed.SemanticError, match="exceeds the available context size"):
        embed.embed_sync({"url": "http://x/embeddings", "model": "m"}, ["a" * 5000])
    assert sizes == [3000], "sent once, cut at max_input_chars"


def test_a_failed_embedding_is_tried_once_and_says_why(monkeypatch):
    import pytest
    posts = []

    def post(url, json, **kw):
        posts.append(url)
        return httpx.Response(503)

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(embed.SemanticError, match="answered 503"):
        embed.embed_sync({"url": "http://x/embeddings", "model": "m"}, ["a"])
    assert len(posts) == 1


def test_a_search_waits_for_its_embedding_as_long_as_advanced_says(tmp_path, monkeypatch):
    """A query's embedding (and the fingerprint the first one asks) waits the Search
    embedding time limit, 60 s unless changed; a build keeps its own longer limit."""
    from celine.kb import semantic
    from kb_fixtures import embed_cfg, fake_embed
    waits = []

    def fake(s, texts):
        waits.append(s.get("timeout"))
        return fake_embed(s, texts)
    monkeypatch.setattr(semantic, "embed_sync", fake)
    embed.forget_fingerprints()
    cfg = embed_cfg()
    semantic.query_vector(cfg, "where did we sail")
    assert waits == [60, 60], "the fingerprint, then the query"

    cfg.settings.general["embed_query_timeout_seconds"] = 5
    waits.clear()
    semantic.query_vector(cfg, "where did we sail")
    assert waits == [5]
    waits.clear()
    semantic.build_embedder(cfg, tmp_path / "none.db").embed(["a passage"])
    assert waits == [None], "a build: the client's own limit"
