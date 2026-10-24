"""Files between the app and its shellbox: the real daemon's PUT and GET /file in a temp
home, served on a local port, against box_files.py and shell__send."""
from __future__ import annotations

from chat_fixtures import user_says

import getpass
import hashlib
import importlib.util
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from celine import attachments, box_files
from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.skills import ToolContext
from celine.skills.shell import Shell
from celine.turns import store as turns

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).parent.parent
KEY = "k" * 24


@pytest.fixture
def home(tmp_path, monkeypatch, stack_key):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("shellboxd_files",
                                                  ROOT / "shellbox" / "shellboxd.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    h = tmp_path / "home"
    h.mkdir()
    d.HOME, d.USER, d.AS_HER = h, getpass.getuser(), {}
    d.KEY_HASH = hashlib.sha256(KEY.encode()).hexdigest()
    server = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    # a load() seeds the address from it
    monkeypatch.setenv("CELINE_SHELL_URL", f"http://127.0.0.1:{server.server_port}")
    stack_key("shell", KEY)
    yield h
    server.shutdown()


@pytest.fixture
def cfg(tmp_path):
    return load()


async def test_an_upload_lands_in_uploads_never_over_another(home, tmp_path):
    src = tmp_path / "game.zip"
    src.write_bytes(os.urandom(3 * 1024 * 1024 + 7))
    first = await box_files.copy_upload(load(), src, "game.zip")
    second = await box_files.copy_upload(load(), src, "game.zip")
    assert first == str(home / "uploads" / "game.zip")
    assert second == str(home / "uploads" / "game (2).zip")
    assert (home / "uploads" / "game.zip").read_bytes() == src.read_bytes()
    assert os.getxattr(home / "uploads" / "game.zip", "user.celine.origin") == b"upload:game.zip"
    assert (home / "uploads" / "game (2).zip").stat().st_mode & 0o777 == 0o644


async def test_nasty_names_land_as_plain_files_in_uploads(home, tmp_path):
    src = tmp_path / "x"
    src.write_bytes(b"x")
    assert await box_files.copy_upload(load(), src, "../../etc/x") == str(home / "uploads" / "x")
    assert await box_files.copy_upload(load(), src, ".bashrc") == str(home / "uploads" / "bashrc")
    assert await box_files.copy_upload(load(), src, "a\x1b\nb") == str(home / "uploads" / "ab")
    assert not (home / ".bashrc").exists()


async def test_a_path_out_of_her_home_is_refused(home, tmp_path):
    src = tmp_path / "x"
    src.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="outside the home folder"):
        await box_files.put(load(), src, "../escape")
    (home / "out").symlink_to(tmp_path)
    with pytest.raises(RuntimeError, match="outside the home folder"):
        await box_files.put(load(), src, "out/escape")
    assert not (tmp_path / "escape").exists()


async def test_her_line_says_where_it_is_in_her_box(cfg):
    line = attachments.render(cfg, [{"role": "user", "content": "here", "attachments": [
        {"id": "a", "filename": "game.zip", "kind": "binary", "sha256": "0" * 64,
         "bytes": 48 * 1048576, "media_type": "application/zip",
         "box_path": "/home/celine/uploads/game.zip"}]}])[0]["content"]
    assert line.endswith("[attachment game.zip, 48.0 MB, application/zip, in your box at "
                         "~/uploads/game.zip]")


async def test_send_attaches_her_file_to_the_conversation(home, cfg, store):
    data = os.urandom(2 * 1024 * 1024)
    (home / "out").mkdir()
    (home / "out" / "report.md").write_bytes(data)
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "send it")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": "c1", "name": "shell__send",
                                                     "arguments": "{}"}])
    sh = Shell(cfg)
    sh.cfg.user = "Joe"
    ctx = ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="c1", store=store,
                      config=cfg, emit=None)
    out = await sh.send(ctx, path=["~/out/report.md"])
    assert not out.is_error and out.content == "Sent Joe report.md (2.0 MB)."
    row = await attachments.get(store, out.display["attachments"][0]["id"])
    assert row["message_id"] == mid
    assert attachments.blob_path(cfg, row["sha256"]).read_bytes() == data
    # its message holds it from now on, and the open conversation is told so
    [ev] = await store.read(lambda c: [json.loads(r["data"]) for r in c.execute(
        "SELECT data FROM events WHERE conversation_id=? AND type='message.attachments'",
        (cid,))])
    assert ev["message_id"] == mid
    assert [(a["id"], a["filename"], a["batch_id"]) for a in ev["attachments"]] == \
        [(row["id"], "report.md", None)]
    missing = await sh.send(ctx, path="nope.txt")
    assert missing.is_error and missing.content == "No file nope.txt in the shellbox home."
    many = await sh.send(ctx, path=[f"f{n}" for n in range(6)])
    assert many.is_error and "zip -r ~/out/files.zip" in many.content


PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4b50000000049454e44ae426082")


async def _look_ctx(store):
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "look")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": "c2", "name": "shell__look",
                                                     "arguments": "{}"}])
    return ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="c2", store=store,
                       config=None, emit=None)


async def test_look_hands_the_image_to_a_model_that_sees(home, cfg, store, monkeypatch):
    (home / "shot.png").write_bytes(PNG)

    async def sees(cfg, ctx):
        return True
    monkeypatch.setattr(attachments, "turn_sees_images", sees)
    out = await Shell(cfg).look(await _look_ctx(store), path="shot.png")
    assert not out.is_error and out.images == [out.display["attachments"][0]["id"]]
    row = await attachments.get(store, out.images[0])
    assert attachments.blob_path(cfg, row["sha256"]).read_bytes() == PNG


async def test_look_describes_it_for_a_model_that_does_not(home, cfg, store, monkeypatch):
    from celine import vision
    (home / "shot.png").write_bytes(PNG)
    asked = []

    async def sees(cfg, ctx):
        return False

    async def ask(llm, png, question, answer_as, **kw):
        asked.append((png, question))
        return type("A", (), {"model": "eyes", "value": "a red dot"})()
    monkeypatch.setattr(attachments, "turn_sees_images", sees)
    monkeypatch.setattr(vision, "ask", ask)
    out = await Shell(cfg).look(await _look_ctx(store), path="shot.png",
                                question="what colour?")
    assert out.images is None and out.content == "shot.png: Description by eyes: a red dot"
    assert asked == [(PNG, "what colour?")]
