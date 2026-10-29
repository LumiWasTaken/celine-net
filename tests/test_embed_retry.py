"""The embedding client shrinks its input when the server says it is too long."""
from __future__ import annotations

import httpx

from celine.kb import embed


def test_a_too_long_answer_retries_with_shorter_input(monkeypatch):
    sizes = []

    def post(url, json, **kw):
        n = max(len(t) for t in json["input"])
        sizes.append(n)
        if n > 1000:
            return httpx.Response(400, json={"error": {
                "message": "input (3000 tokens) exceeds the available context size"}})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    monkeypatch.setattr(httpx, "post", post)
    out = embed.embed_sync({"url": "http://x/embeddings", "model": "m"}, ["a" * 5000])
    assert out == [[1.0]] and sizes == [3000, 1500, 750]


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
