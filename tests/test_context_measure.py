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


def test_a_result_is_capped_absolutely_and_keeps_its_start_and_end():
    from types import SimpleNamespace
    from celine.skills import ToolResult
    from celine.turns import tokens, tools
    assert tokens.result_cap(65_536) == 9_830, "15 % of the window, under 10,000"
    assert tokens.result_cap(262_144) == 10_000, "the absolute cap on a large window"
    assert tokens.result_cap(262_144, SimpleNamespace(context={"result_tokens": 4000})) \
        == 4000, "the owner's number in context.toml"
    text = "".join(f"line {i} of the build log\n" for i in range(2000))
    out = tools._capped(ToolResult(content=text, images=["a1"]), {"max_output_tokens": 500})
    head, rest = out.content.split("\n\n[cut: ", 1)
    line, tail = rest.split("\n\n", 1)
    assert head.startswith("line 0 of") and tail.rstrip().endswith("line 1999 of the build log")
    assert line.endswith("tokens left out here, between the start and the end. Page through "
                         "it, or read the part you need from its file.]")
    assert out.images == ["a1"]
    assert tokens.estimate(head + tail, "tool") <= 520
