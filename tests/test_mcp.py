"""MCP: tools appear, calls work, a crashed server comes back, a connector's records
land in the archive."""
from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from celine.mcp import McpServerConfig

from celine.boot import load
from celine.kb import KBs
from celine.kb.chat import ChatKB
from celine.mcp import McpSkill, Supervisor, cache_tools, cached_tools
from celine.skills import Registry, ToolContext

from chat_fixtures import until

pytestmark = pytest.mark.anyio

SERVER = Path(__file__).with_name("mcp_echo_server.py")


async def _noop(_: str) -> None:
    pass


def _ctx(store):
    return ToolContext(conversation_id="c", turn_id="t", tool_call_id="x",
                       store=store, config=None, emit=_noop)


def _cfg(sid="echo", **kw):
    c = load()
    c.mcp = {sid: McpServerConfig(**{"id": sid, "enabled": True, "transport": "stdio",
                                     "command": sys.executable, "args": [str(SERVER)],
                                     "startup_timeout_seconds": 20, **kw})}
    return c


async def test_tools_appear_calls_round_trip_and_a_crash_comes_back(store):
    reg = Registry()
    sup = Supervisor(_cfg(), store, reg, ping_seconds=0.5)
    async with anyio.create_task_group() as tg:
        await sup.start(tg)
        skill = sup.skills["echo"]
        await until(lambda: skill.status == "up")
        assert reg.tools["echo__echo"].annotations.read_only and reg.tools["echo__send"].mutating
        out = await reg.tools["echo__echo"].fn(_ctx(store), text="hello")
        assert out.content == "hello" and not out.is_error
        assert "echo" in [t["name"] for t in await cached_tools(store, "echo")]
        assert (await reg.tools["echo__crash"].fn(_ctx(store))).is_error
        await until(lambda: skill.ups >= 2 and skill.status == "up")
        assert (await reg.tools["echo__echo"].fn(_ctx(store), text="again")).content == "again"
        tg.cancel_scope.cancel()


class _KBs:
    """What the supervisor and resolve_refs need of KBs: get()."""

    def __init__(self, kb):
        self.kb = kb

    def get(self, kid):
        return self.kb if kid == self.kb.id else None

    resolve_refs = KBs.resolve_refs


async def test_connector_records_render_as_citable_archive_lines(store, tmp_path):
    kb = ChatKB(id="chatlink", title="Chat link", platform="discord", path=tmp_path / "c.db")
    reg = Registry()
    sup = Supervisor(_cfg("chatlink", kb="chatlink"), store, reg, kbs=_KBs(kb),
                     ping_seconds=0.5)
    async with anyio.create_task_group() as tg:
        await sup.start(tg)
        await until(lambda: sup.skills["chatlink"].status == "up")
        # a connector only reads: its one write is not offered
        assert "chatlink__send" not in reg.tools
        out = await reg.tools["chatlink__read_messages"].fn(_ctx(store), chat="c9")
        assert not out.is_error
        refs = [ln.split("]")[0].split("[")[1] for ln in out.content.splitlines()
                if ln.lstrip().startswith("[")]
        assert len(refs) == 2 and all(r.startswith("chatlink:") for r in refs)
        resolved = _KBs(kb).resolve_refs(refs)
        assert [m["text"] for m in resolved.values()] == ["first", "second"]
        tg.cancel_scope.cancel()


async def test_cached_connector_tools_show_while_the_server_starts(store):
    reg = Registry()
    await cache_tools(store, "chatlink", [
        {"name": "read_messages", "description": "d", "schema": {},
         "annotations": {"readOnlyHint": True, "destructiveHint": False}},
        {"name": "send", "description": "d", "schema": {}, "annotations": None}])
    sup = Supervisor(_cfg("chatlink", kb="chatlink"), store, reg, ping_seconds=0.5)
    sup._register(sup.skills.setdefault("chatlink", McpSkill(sup.cfg.mcp["chatlink"])),
                  await cached_tools(store, "chatlink"))
    assert "chatlink__read_messages" in reg.tools and "chatlink__send" not in reg.tools


async def test_an_image_in_a_result_is_kept_as_an_attachment_of_the_call(store):
    """An image part is saved for the call and its id goes in ToolResult.images, as a
    browser shot does; the text keeps no placeholder for it."""
    import base64
    from mcp.types import CallToolResult, ImageContent, TextContent
    from chat_fixtures import PNG, user_says
    from celine import attachments
    from celine.conversations import store as conversations, tree
    from celine.turns import store as turns

    class Client:
        async def call_tool(self, name, args):
            return CallToolResult(content=[
                TextContent(type="text", text="the screen"),
                ImageContent(type="image", data=base64.b64encode(PNG).decode(),
                             mimeType="image/png")])

    sup = Supervisor(_cfg(), store, Registry())
    skill = McpSkill(sup.cfg.mcp["echo"])
    skill.client, skill.status = Client(), "up"
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "look")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": "x", "name": "echo__shot",
                                                    "arguments": "{}"}])
    out = await sup._call(_ctx(store), skill, "shot", {})
    assert not out.is_error and out.content.startswith("the screen")
    assert "[image]" not in out.content and len(out.images) == 1
    row = await attachments.get(store, out.images[0])
    assert row["media_type"] == "image/png" and row["filename"] == "shot.png"
    assert attachments.blob_path(sup.cfg, row["sha256"]).read_bytes() == PNG


async def test_a_resource_in_a_result_is_kept_as_a_file_of_the_call(store, monkeypatch):
    """A blob or text resource is saved for the call (its row shows it) and named to the
    model, its media type from its name when the server says only octet-stream; one over
    max_attachment_bytes is named as not kept."""
    import base64
    from mcp.types import (BlobResourceContents, CallToolResult, EmbeddedResource,
                           TextResourceContents)
    from chat_fixtures import user_says
    from celine.conversations import store as conversations, tree
    from celine.turns import store as turns

    class Client:
        async def call_tool(self, name, args):
            return CallToolResult(content=[
                EmbeddedResource(type="resource", resource=BlobResourceContents(
                    uri="file:///Users/her/Desktop/my%20notes.zip",
                    mimeType="application/zip",
                    blob=base64.b64encode(b"PK\x03\x04zip").decode())),
                EmbeddedResource(type="resource", resource=TextResourceContents(
                    uri="file:///tmp/a.txt", text="hello")),
                EmbeddedResource(type="resource", resource=BlobResourceContents(
                    uri="file:///tmp/report.pdf", mimeType="application/octet-stream",
                    blob=base64.b64encode(b"%PDF-1.7").decode())),
                EmbeddedResource(type="resource", resource=BlobResourceContents(
                    uri="file:///big.bin", blob=base64.b64encode(b"x" * 64).decode()))])

    sup = Supervisor(_cfg(), store, Registry())
    real = sup.cfg.r
    monkeypatch.setattr(sup.cfg, "r",
                        lambda k: 32 if k == "max_attachment_bytes" else real(k))
    sup.cfg.user = "Joe"
    skill = McpSkill(sup.cfg.mcp["echo"])
    skill.client, skill.status = Client(), "up"
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "fetch")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": "x", "name": "echo__get",
                                                    "arguments": "{}"}])
    out = await sup._call(_ctx(store), skill, "get", {})
    assert out.images is None and "[resource]" not in out.content
    assert "my notes.zip (7 bytes), attached to this conversation for Joe." in out.content
    assert "big.bin is 0.0 MB. The limit is 0 MB. It was not kept." in out.content
    rows = await store.read(lambda c: [tuple(r) for r in c.execute(
        "SELECT a.filename, b.media_type FROM attachments a JOIN blobs b USING (sha256) "
        "WHERE a.tool_call_id='x' ORDER BY a.rowid")])
    assert rows == [("my notes.zip", "application/zip"), ("a.txt", "text/plain"),
                    ("report.pdf", "application/pdf")]


async def test_a_url_server_stays_down_with_its_reason_and_comes_back_by_itself(store):
    reg = Registry()
    cfg = _cfg("away", transport="http", command=None, args=[], url="http://127.0.0.1:9/mcp",
               startup_timeout_seconds=1, max_restarts=0)
    sup = Supervisor(cfg, store, reg, ping_seconds=0.5)
    connect = sup._connect
    async with anyio.create_task_group() as tg:
        await sup.start(tg)
        skill = sup.skills["away"]
        await until(lambda: skill.restarts >= 2)
        assert skill.status == "down", "past max_restarts a url server is not dead"
        assert skill.error == ("The away server did not answer at 127.0.0.1:9. Start it, or "
                               "fix the address.")
        here = _cfg().mcp["echo"]                   # its host answers again
        sup._connect = lambda _cfg: connect(here)
        skill.kick.set()
        await until(lambda: skill.status == "up")
        assert skill.error is None and "away__echo" in reg.tools
        tg.cancel_scope.cancel()


async def test_a_server_the_app_starts_is_dead_past_max_restarts(store):
    reg = Registry()
    sup = Supervisor(_cfg("gone", command="/nonexistent/server", max_restarts=0), store, reg,
                     ping_seconds=0.5)
    async with anyio.create_task_group() as tg:
        await sup.start(tg)
        await until(lambda: sup.skills["gone"].status == "dead")
        tg.cancel_scope.cancel()


def test_an_mcp_argument_named_like_a_python_keyword_keeps_its_name():
    from celine.turns.tools import _takes_any

    async def mcp(ctx, **args): ...
    async def own(ctx, in_: str): ...
    assert _takes_any(mcp) and not _takes_any(own)


def test_a_server_named_like_a_built_in_skill_is_refused_with_its_reason():
    """[servers.notebook] would replace the notebook in the registry: it is left out, and
    the config's problems say why. Another name is read."""
    from celine.mcp import read_servers
    from celine.skills import META
    problems = []
    raw = {"servers": {"notebook": {"command": "x"}, META: {"command": "x"},
                       "my-tools": {"command": "x"}}}
    assert list(read_servers("mcp.toml", raw, problems, {})) == ["my-tools"]
    assert [p.message for p in problems] == [
        "[servers.notebook] is the name of a built-in skill; that server is not started",
        f"[servers.{META}] is the name of a built-in skill; that server is not started"]
