"""Attachments: storage, the size limit, and GC that never drops an attached file."""
from __future__ import annotations

from chat_fixtures import PNG, upload, user_says

import os
from datetime import datetime, timedelta, timezone

import pytest

from celine.conversations import store as conversations, tree, view
from celine import attachments
from celine.boot import load
from celine.errors import TooLarge
from celine.turns import store as turns

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)      # the fixed clock of the gc tests
LATER = datetime(2100, 1, 1, tzinfo=timezone.utc)   # every row made now is KEEP old

@pytest.fixture
def cfg():
    return load()


async def test_kinds_dedupe_and_size_limit(cfg, store):
    img = await upload(cfg, store, PNG, "dot.png", "image/png")
    txt = await upload(cfg, store, b"hello\n", "notes.txt", None)
    assert (img["kind"], txt["kind"]) == ("image", "text")
    assert attachments.blob_path(cfg, img["sha256"]).read_bytes() == PNG
    again = await upload(cfg, store, PNG, "dot2.png", "image/png")
    assert again["sha256"] == img["sha256"] and again["id"] != img["id"]
    cfg.settings.general["max_attachment_bytes"] = 4
    with pytest.raises(TooLarge):
        await upload(cfg, store, b"12345", "big.txt", None)


async def test_an_attached_file_is_never_dropped(cfg, store):
    kept = await upload(cfg, store, b"keep", "k.txt", None)
    stale = await upload(cfg, store, b"stale", "s.txt", None)
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "x", attachment_ids=[kept["id"]])
    snap = await view.snapshot(store, cid)
    assert [x["filename"] for x in snap["messages"][0]["attachments"]] == ["k.txt"]
    with pytest.raises(ValueError):
        await user_says(store, cid, "again", attachment_ids=[kept["id"]])
    assert not await attachments.delete_draft(store, kept["id"])
    # a browser shot is its call's alone: not its image, not a draft, kept by GC
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": "c1", "name": "browser__look",
                                              "arguments": "{}"}])
    await attachments.save_generated(cfg, store, PNG, "main-plain.png", "image/png", "c1",
                                     in_message=False)
    assert await attachments.images(store, cid) == []
    assert await attachments.gc(cfg, store, at=LATER) == 1
    assert attachments.blob_path(cfg, kept["sha256"]).exists()
    assert not attachments.blob_path(cfg, stale["sha256"]).exists()
    assert await store.read(lambda c: c.execute("SELECT 1 FROM blobs WHERE sha256=?",
                                                (stale["sha256"],)).fetchone()) is None


async def test_an_image_a_result_holds_goes_to_the_model_with_it(cfg, store):
    """user_documents__read on an image: the result carries the image (ToolResult.images)
    and the context sends it as an image part of that tool message."""
    from fake_backend import FakeBackend
    from celine.skills import ToolResult
    from celine.turns import context
    from celine.turns.verify import shown_refs
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "what is in the photo?")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": "c1", "name": "user_documents__read",
                                                     "arguments": "{}"}])
    row = await attachments.save_generated(cfg, store, PNG, "dot.png", "image/png", "c1",
                                           in_message=False)
    await turns.finish_tool_call(store, cid, mid, "c1", "user_documents__read", ToolResult(
        content="[documents:7] (description of the image) a dot", images=[row["id"]]), "ok")
    built = await context.build(store, cfg, FakeBackend(), cid, system="S", tools=[])
    tool = next(m for m in built.messages if m["role"] == "tool")
    assert tool["tool_call_id"] == "c1"
    assert [p["type"] for p in tool["content"]] == ["text", attachments.IMAGE]
    assert shown_refs(built.messages) == {"documents:7"}


async def _made_at(store, sha: str, t: datetime) -> None:
    """The blob and its attachments as if stored at `t`."""
    ts = t.isoformat(timespec="milliseconds")
    await store.write(lambda c: (
        c.execute("UPDATE blobs SET created_at=? WHERE sha256=?", (ts, sha)),
        c.execute("UPDATE attachments SET created_at=? WHERE sha256=?", (ts, sha))))


async def test_gc_keeps_drafts_and_unused_files_for_keep(cfg, store):
    """A draft keeps its file for KEEP; a discarded one's file stays KEEP from its upload;
    a sent one's file stays as long as its message; a half-written upload goes once
    untouched for KEEP."""
    draft = await upload(cfg, store, b"draft", "d.txt", None)
    gone = await upload(cfg, store, b"discarded", "x.txt", None)
    sent = await upload(cfg, store, b"sent", "s.txt", None)
    cid = await conversations.create(store)
    await user_says(store, cid, "x", attachment_ids=[sent["id"]])
    assert await attachments.delete_draft(store, gone["id"])
    for row in (draft, gone, sent):
        await _made_at(store, row["sha256"], T0)
    tmp = cfg.blobs_dir / "tmp" / "half"
    tmp.write_bytes(b"half")
    os.utime(tmp, (T0.timestamp(), T0.timestamp()))

    def there(row) -> bool:
        return attachments.blob_path(cfg, row["sha256"]).exists()

    assert await attachments.gc(cfg, store, at=T0 + attachments.KEEP - timedelta(days=1)) == 0
    assert there(draft) and there(gone) and there(sent) and tmp.exists()
    assert await attachments.get(store, draft["id"]) is not None
    assert await attachments.gc(cfg, store, at=T0 + attachments.KEEP + timedelta(days=1)) == 2
    assert not there(draft) and not there(gone) and there(sent) and not tmp.exists()
    assert await attachments.get(store, draft["id"]) is None
    assert await attachments.gc(cfg, store, at=LATER) == 0 and there(sent)


async def test_a_file_whose_rows_fail_is_not_left_behind(cfg, store):
    with pytest.raises(KeyError):
        await attachments.save_generated(cfg, store, PNG, "x.png", "image/png", "no-call")
    assert list((cfg.blobs_dir / "tmp").iterdir()) == []
    assert not any(p.is_file() for p in cfg.blobs_dir.rglob("*"))
