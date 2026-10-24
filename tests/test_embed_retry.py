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
