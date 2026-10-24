"""The picture block's server side: a picture has its size before it loads, Retry makes one
item again in place, and a picture beat made again changes where it is."""
from __future__ import annotations

import pytest

from celine import attachments
from celine.comfyui import batches as batches_mod
from celine.comfyui.presets import ImageError
from test_comfyui import _call, app  # noqa: F401  (the batch runner on a fake ComfyUI)

pytestmark = pytest.mark.anyio


async def test_retry_makes_one_failed_item_again_and_refuses_the_rest(app):  # noqa: F811
    app.fake.fail = {"p1"}
    b = await app.batches.create("images", [{"prompt": "a"}, {"prompt": "b", "preset": "nope"}])
    b = await app.batches.run(b["id"])
    assert [i["state"] for i in b["items"]] == ["failed", "failed"]
    with pytest.raises(ImageError, match="no preset 'nope'"):
        await app.batches.retry(b["id"], 1)
    app.fake.fail, app.fake.hang = set(), True
    b = await app.batches.retry(b["id"], 0)
    assert b["state"] == "running" and b["items"][0]["state"] == "waiting"
    with pytest.raises(ImageError, match="still running"):
        await app.batches.retry(b["id"], 0)
    assert await app.batches.stop(b["id"]) is True
    assert (await batches_mod.get(app.store, b["id"]))["items"][0]["state"] == "stopped"
    app.fake.hang = False
    await app.batches.retry(b["id"], 0)
    await app.batches.wait(b["id"])
    b = await batches_mod.get(app.store, b["id"])
    assert b["state"] == "done" and b["items"][0]["state"] == "done"
    assert "error" not in b["items"][0] and b["items"][0]["attachment"]["width"] == 832
    with pytest.raises(ImageError, match="Only a failed or stopped item"):
        await app.batches.retry(b["id"], 0)


async def test_her_picture_has_its_size_on_her_message(app, store):  # noqa: F811
    from celine.conversations import store as conversations
    cid = await conversations.create(store)
    _, mid = await _call(store, cid, "call1")
    b = await app.batches.create("images", [{"prompt": "a"}], conversation_id=cid,
                                 tool_call_id="call1")
    await app.batches.run(b["id"])
    rows = await store.read(lambda c: attachments.of_message(c, mid))
    assert [(r["width"], r["height"]) for r in rows] == [(832, 1216)]


async def test_retry_on_her_message_without_a_speaker_runs(app, store):  # noqa: F811
    from celine.conversations import store as conversations
    cid = await conversations.create(store)
    await _call(store, cid, "call1")
    app.fake.fail = {"p1"}
    b = await app.batches.create("images", [{"prompt": "a"}], conversation_id=cid,
                                 tool_call_id="call1")
    b = await app.batches.run(b["id"])
    assert b["items"][0]["state"] == "failed"
    app.fake.fail = set()
    await app.batches.retry(b["id"], 0)
    await app.batches.wait(b["id"])
    assert (await batches_mod.get(app.store, b["id"]))["state"] == "done"
