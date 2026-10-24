"""What Bastet sends a judge stays inside its budget, with a marker at every cut
(guard/caps.py)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from celine.guard import caps


def test_a_long_intent_keeps_its_tail_and_marks_the_cut():
    lines = ["Owner: plan the trip"] + [f"{{agent_name}} said: {'x' * 5000}"] * 10 + \
        [f"{{agent_name}} called shell__run {'y' * 9000}", "{{agent_name}} said: the newest words"]
    got = caps.intent(lines)
    assert len(got) < caps.INTENT + 100
    assert got.endswith("the newest words") and "characters left out" in got


def test_long_arguments_stay_json_and_keep_the_short_values_whole():
    a = {"url": "https://example.org/a", "content": "z" * 20000, "to": "user"}
    got = caps.args(a)
    assert len(got) < caps.ARGS + 200
    back = json.loads(got)
    assert back["url"] == a["url"] and back["to"] == "user"
    assert "characters left out" in back["content"]


def test_a_huge_argument_list_falls_back_to_head_and_tail_text():
    a = {"steps": [{"text": "w" * 1000} for _ in range(40)]}
    got = caps.args(a)
    assert len(got) <= caps.ARGS + 60 and "characters left out" in got


def test_many_steps_keep_the_first_and_the_last_around_a_marker():
    steps = [f"step {i} " + "s" * 500 for i in range(30)]
    got = caps.lines(steps)
    assert got[0] == steps[0] and got[-1] == steps[-1]
    assert any("characters left out" in x for x in got)
    assert sum(len(x) for x in got) < caps.ARGS + 100


@pytest.mark.anyio
async def test_streamed_logprobs_reach_the_judges_probability():
    """llama-server streams the logprobs per chunk; Done.raw carries them, so P(D) is a
    probability, not the letter's 0 or 1."""
    import httpx

    from celine.backends.base import Done
    from celine.backends.openai import OpenAIBackend
    from celine.guard.judge import p_deny
    sse = ('data: {"choices":[{"delta":{"content":"A"},"logprobs":{"content":[{"token":"A",'
           '"logprob":-0.6,"top_logprobs":[{"token":"A","logprob":-0.6},{"token":"D",'
           '"logprob":-0.8}]}]}}]}\n\n'
           'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n')
    done = [e async for e in OpenAIBackend._parse(SimpleNamespace(cfg=SimpleNamespace(label="gpu"), model="m"),
                                                 httpx.Response(200, text=sse))
            if isinstance(e, Done)][0]
    p = p_deny(done.raw, "A")
    assert 0.4 < p < 0.5
