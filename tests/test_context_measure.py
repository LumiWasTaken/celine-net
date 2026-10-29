"""Each step records its time to the first token, its prompt tokens and the prompt
tokens the server read from its cache (turns/runner.py _record_usage), in `requests`
beside the request (the reply's What she saw)."""
from __future__ import annotations

import pytest

from test_llm_queue import I, routed
from test_turn_loop import Replies, _run

from celine.backends.base import Done, TextDelta, Usage
from celine.backends.openai import cached
from celine.boot import load
from celine.conversations import store as conversations
from celine.skills import Registry
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


class Timed(Replies):
    """A backend that measured its first token and kept the request it sent."""
    first_token = 0.25
    request_sent = {"provider": "p", "model": "m", "body": {"messages": []}}


async def test_a_step_records_its_first_token_prompt_and_cached_tokens(store):
    cfg = load()
    cid = await conversations.create(store)
    b = Timed([[TextDelta("hi"), Done(reason="stop", usage=Usage(1200, 5, cache_read=900))]])
    await _run(store, cfg, Registry(cfg), cid, b)
    mid = await store.read(lambda c: c.execute(
        "SELECT id FROM messages WHERE role='assistant'").fetchone()[0])
    [row] = await turns.requests_of(store, mid)
    assert (row["prompt_tokens"], row["cache_read"], row["first_token_ms"]) == (1200, 900, 250)


async def test_the_queued_backend_times_the_first_token(tmp_path, monkeypatch):
    llm, _ = routed(tmp_path, monkeypatch, {"chat": {"chain": ["main/big"]}})
    b = llm.bind(role="chat", priority=I, conversation_id="c1")
    assert b.first_token is None
    async for _ in b.stream([{"role": "user", "content": "hi"}], []):
        pass
    assert b.first_token is not None and b.first_token >= 0


def test_cached_tokens_read_from_openai_and_llama_cpp_shapes():
    assert cached({"usage": {"prompt_tokens_details": {"cached_tokens": 7}}}) == 7
    assert cached({"usage": {"prompt_tokens": 9}, "timings": {"cache_n": 4}}) == 4
    assert cached({"usage": {"prompt_tokens": 9}}) == 0
