"""Against a real model, opt-in: CELINE_LIVE=http://127.0.0.1:11434/v1
CELINE_LIVE_MODEL=my-model uv run pytest -m live"""
from __future__ import annotations

import os

import pytest

from celine.backends.base import Done, TextDelta
from celine.backends.openai import LlamaServerBackend
from celine.config import BackendConfig

pytestmark = [pytest.mark.anyio, pytest.mark.live]

BASE = os.environ.get("CELINE_LIVE")
if not BASE:
    pytest.skip("set CELINE_LIVE to run against a real backend", allow_module_level=True)


async def test_a_real_model_streams_and_carries_the_history():
    backend = LlamaServerBackend(BackendConfig(
        id="live", kind="llama-server", label="llama.cpp", base_url=BASE.rstrip("/"),
        model=os.environ.get("CELINE_LIVE_MODEL", "my-model"),
        params={"temperature": 0.2, "max_tokens": 128},
        raw={"llama": {"top_k": 40, "min_p": 0.05, "cache_prompt": True}}), timeout=300.0)
    events = [ev async for ev in backend.stream([
        {"role": "user", "content": "My favourite colour is teal. Remember it."},
        {"role": "assistant", "content": "Noted, teal."},
        {"role": "user", "content": "What is my favourite colour? One word."}], [])]
    assert isinstance(events[-1], Done) and events[-1].reason in ("stop", "length")
    assert "teal" in "".join(e.text for e in events if isinstance(e, TextDelta)).lower()
