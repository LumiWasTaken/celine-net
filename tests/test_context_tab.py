"""The Context tab: the notes it carries by need count in the Notebook row, not in the
conversation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from celine import boot
from celine.conversations import store as conversations
from celine.turns import loads
from compaction_fixtures import Model
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


async def test_notes_carried_by_need_count_as_notes(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    note = cfg.her.notebook / "lore" / "sea.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("The sea by the house is grey in the morning. " * 60)
    cid = await conversations.create(store)
    await loads.touch(store, cid, "note", ["lore/sea.md"])
    app = make_app(cfg, store)
    app.state.services.dispatcher = SimpleNamespace(
        notify=lambda: None, backend_factory=lambda: Model(window=65_536),
        bound=lambda cid, **_: Model(window=65_536), llm=None, _tg=None)
    async with client(app) as api:
        ctx = (await api.get(f"/api/conversations/{cid}/context")).json()
    blocks = {b["name"]: b["tokens"] for b in ctx["blocks"]}
    listed = sum(n["tokens"] for n in ctx["notes"] if n["state"] == "included")
    assert any(n["path"] == "lore/sea.md" and n["state"] == "included" for n in ctx["notes"])
    assert listed > 300 and blocks["notes"] >= listed
    assert blocks["history"] < listed, "the note is not counted as the conversation"
