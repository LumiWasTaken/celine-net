"""The OpenRouter probe asks an endpoint that needs the key, so a bad key shows."""
from __future__ import annotations

import httpx
import pytest

from celine.backends.router import Router
from celine.boot import load
from settings_fixtures import scratch

pytestmark = pytest.mark.anyio


async def test_a_rejected_openrouter_key_is_the_provider_state(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch, roles={}, providers={
        "or": {"kind": "openrouter", "label": "OpenRouter",
               "base_url": "https://openrouter.ai/api/v1", "models": {"a/b": {"caps": ["chat"]}}}})
    asked = []

    def reply(request):
        asked.append(request.url.path)
        return httpx.Response(401 if request.url.path.endswith("/key") else 200, json={})
    t = httpx.MockTransport(reply)
    st = await Router(load(), None, transport=t, sync_transport=t).probe("or", max_age=0)
    assert asked == ["/api/v1/key"]
    assert st.reachable is False and st.error == "OpenRouter rejected the API key."
