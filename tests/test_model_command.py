"""/model fails with the provider's reason when it does not answer: no quiet fall back to
the saved list (an interactive command)."""
from __future__ import annotations

import pytest

from settings_fixtures import client, make_app, scratch

from celine import boot
from celine.api import commands as commands_api
from celine.conversations import store as conversations
from celine.settings import kinds

pytestmark = pytest.mark.anyio


async def test_model_says_why_the_provider_did_not_answer(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(commands_api.router)

    async def down(*a, **k):
        raise OSError("refused")

    async def check(*a, **k):
        return {"sentence": "llama-swap did not answer at 127.0.0.1:11434."}
    monkeypatch.setattr(kinds, "discover", down)
    monkeypatch.setattr(kinds, "check", check)
    cid = await conversations.create(store)
    async with client(app) as c:
        r = await c.post(f"/api/conversations/{cid}/slash-commands/model/run",
                         json={"text": "big"})
    assert r.status_code == 502
    assert r.json()["error"] == {"code": "unreachable",
                                 "message": "llama-swap did not answer at 127.0.0.1:11434."}


async def test_think_suggests_the_words_of_the_conversations_model(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    app = make_app(boot.load(), store)
    app.include_router(commands_api.router)
    cid = await conversations.create(store)
    async with client(app) as c:
        cmds = (await c.get(f"/api/slash-commands?conversation={cid}")).json()["commands"]
    [think] = [x for x in cmds if x["name"] == "think"]
    assert think["args"][0]["suggestions"] == ["off", "low", "medium", "xhigh"]
