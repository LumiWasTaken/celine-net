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


def picture(w: int, h: int, fmt: str = "PNG", **kw) -> bytes:
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 40, 40)).save(out, fmt, **kw)
    return out.getvalue()


async def test_a_picture_keeps_its_size_and_one_smaller_copy_per_width(cfg, store):
    from PIL import Image
    big = await upload(cfg, store, picture(2000, 1000), "wide.png", "image/png")
    size = await store.read(lambda c: tuple(c.execute(
        "SELECT width, height FROM blobs WHERE sha256=?", (big["sha256"],)).fetchone()))
    assert size == (2000, 1000)
    # a phone photo turned by its EXIF orientation is as tall as it is shown
    exif = Image.Exif()
    exif[0x0112] = 6
    turned = await upload(cfg, store, picture(400, 300, "JPEG", exif=exif), "p.jpg",
                          "image/jpeg")
    assert await store.read(lambda c: tuple(c.execute(
        "SELECT width, height FROM blobs WHERE sha256=?", (turned["sha256"],)).fetchone())) \
        == (300, 400)
    assert [attachments.snap(n) for n in (1, 320, 321, 900, 5000)] == [320, 320, 640, 1280,
                                                                       1280]
    small = attachments.resized(cfg, big["sha256"], 640)
    assert small.name == f"{big['sha256']}.w640.webp"
    with Image.open(small) as im:
        assert (im.format, im.size) == ("WEBP", (640, 320))
    made = small.stat().st_mtime_ns
    assert attachments.resized(cfg, big["sha256"], 640) == small
    assert small.stat().st_mtime_ns == made                    # made once
    # never wider than the picture, never a copy of a file that is no picture
    dot = await upload(cfg, store, PNG, "dot.png", "image/png")
    assert attachments.resized(cfg, dot["sha256"], 320) == attachments.blob_path(
        cfg, dot["sha256"])
    # the copy goes with its blob
    await attachments.delete_draft(store, big["id"])
    await _made_at(store, big["sha256"], T0)
    await attachments.gc(cfg, store, at=LATER)
    assert not small.exists() and not attachments.blob_path(cfg, big["sha256"]).exists()


async def test_a_model_gets_a_picture_at_most_wire_edge_and_its_real_type(cfg, store):
    import base64
    import io
    from PIL import Image
    from celine import vision
    tall = await upload(cfg, store, picture(1500, 3000), "tall.png", "image/png")
    path, media = attachments.for_model(cfg, tall["sha256"], "image/png")
    with Image.open(path) as im:
        assert (media, im.size) == ("image/webp", (784, 1568))
    dot = await upload(cfg, store, PNG, "dot.png", "image/png")
    assert attachments.for_model(cfg, dot["sha256"], "image/png") == \
        (attachments.blob_path(cfg, dot["sha256"]), "image/png")
    part = {"type": attachments.IMAGE, "attachment": {**tall, "size": (1500, 3000)}}
    sent = await attachments.Images(cfg, store, None, "x")._image(part)
    head, b64 = sent["image_url"]["url"].split(",", 1)
    assert head == "data:image/webp;base64"
    with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
        assert im.size == (784, 1568)
    jpeg = picture(8, 8, "JPEG")
    [msg] = vision.messages(jpeg, "what?", "text")
    assert msg["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_a_file_whose_rows_fail_is_not_left_behind(cfg, store):
    with pytest.raises(KeyError):
        await attachments.save_generated(cfg, store, PNG, "x.png", "image/png", "no-call")
    assert list((cfg.blobs_dir / "tmp").iterdir()) == []
    assert not any(p.is_file() for p in cfg.blobs_dir.rglob("*"))
