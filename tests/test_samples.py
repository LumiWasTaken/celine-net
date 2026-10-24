""""Love this": its reply becomes a voice sample in samples.md (always loaded,
newest 20, older ones under "## earlier" which it does not carry), the message says
it is one, and Delete takes it out again."""
from __future__ import annotations

import asyncio

import pytest

from celine import boot
from celine.api import notebook as notebook_api
from celine.conversations import samples, store as conversations, tree, view
from celine.skills.notebook import carried_text
from chat_fixtures import user_says
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


def test_the_note_keeps_the_newest_and_folds_the_rest():
    body = None
    for i in range(samples.KEEP + 2):
        body = samples.added(body, samples.entry(f"line {i}", "2026-10-10", "my-model"))
    head, _, earlier = body.partition("\n## earlier\n")
    assert head.startswith("load: always\nHow I talk: replies {{username}} marked.")
    assert head.count("### ") == samples.KEEP == 8 and "line 9" in head and "line 2\n" in head
    assert earlier.count("### ") == 2 and "line 1\n" in earlier and "line 0\n" in earlier
    assert "line 0" not in carried_text(body), "the earlier ones are not in its context"
    gone = samples.removed(body, samples.entry("line 0", "2026-10-10", "my-model"))
    assert "line 0" not in gone and "line 1\n" in gone and gone.count("### ") == samples.KEEP + 1


async def test_love_this_over_http(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(notebook_api.router)
    cid = await conversations.create(store)
    umid, tid, _ = await user_says(store, cid, "how are you")
    await store.write(lambda c: c.execute("UPDATE turns SET state='done', model='my-model' WHERE id=?", (tid,)))
    hers = await store.write(lambda c: tree.append(
        c, store, cid, umid, "assistant", "finally. get ready for a busy day, friend.", "model", turn_id=tid))
    url = f"/api/conversations/{cid}/messages/{hers['id']}/voice-sample"
    async with client(app) as c:
        assert (await c.put(url)).json() == {"sample": True}
        note = (cfg.her.notebook / samples.PATH).read_text()
        assert "load: always" in note and ", my-model\nfinally. get ready" in note
        msgs = (await view.snapshot(store, cid))["messages"]
        assert msgs[-1]["sample"] is True and "sample" not in msgs[0]
        assert (await c.put(url)).json() == {"sample": True}, "twice is once"
        assert (cfg.her.notebook / samples.PATH).read_text().count("finally.") == 1
        assert (await c.delete(url)).json() == {"sample": False}
        assert "finally." not in (cfg.her.notebook / samples.PATH).read_text()
        assert (await view.snapshot(store, cid))["messages"][-1]["sample"] is False
        versions = (await c.get("/api/notebook/versions", params={"path": samples.PATH})).json()["versions"]
        assert len(versions) == 1, "the text before the delete is kept"
        assert (await c.put(f"/api/conversations/{cid}/messages/{umid}/voice-sample")).status_code == 400
        both = await asyncio.gather(*[c.put(url) for _ in range(4)])
        assert [r.status_code for r in both] == [200] * 4, "quick clicks are one"
        assert (cfg.her.notebook / samples.PATH).read_text().count("finally.") == 1
