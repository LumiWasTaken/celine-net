"""A sentence built outside a turn names it: an error that still holds {{name}}
(errors.py) and the slash command menu (commands/, api/commands.py)."""
from __future__ import annotations

import pytest

from celine import boot
from celine.api import commands
from celine.conversations import store as conversations
from celine.errors import CelineError
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


async def test_a_sentence_outside_a_turn_names_her(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(commands.router)

    @app.post("/test/{cid}/busy")
    async def busy(cid: str):
        raise CelineError("busy", "{{name}} is still answering.", 409, {"reason": "{{name}}"})

    mine = await conversations.create(store)
    async with client(app) as c:
        err = (await c.post(f"/test/{mine}/busy")).json()["error"]
        assert (err["message"], err["detail"]) == (f"{cfg.name} is still answering.",
                                                   {"reason": cfg.name})

        def usage(r):
            return {x["name"]: x for x in r.json()["commands"]}["usage"]["description"]
        assert f"in {cfg.name}'s context" in usage(
            await c.get(f"/api/slash-commands?conversation={mine}"))
        assert f"in {cfg.name}'s context" in usage(await c.get("/api/slash-commands"))
