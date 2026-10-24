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
        # the one write is not Celine's tool; the bridge reaches it through call()
        assert "chatlink__send" not in reg.tools
        out = await reg.tools["chatlink__read_messages"].fn(_ctx(store), chat="c9")
        assert not out.is_error
        refs = [ln.split("]")[0].split("[")[1] for ln in out.content.splitlines()
                if ln.lstrip().startswith("[")]
        assert len(refs) == 2 and all(r.startswith("chatlink:") for r in refs)
        resolved = _KBs(kb).resolve_refs(refs)
        assert [m["text"] for m in resolved.values()] == ["first", "second"]
        res = await sup.call("chatlink", "send", {"text": "hi"})
        assert res.content[0].text == "sent: hi"
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


async def test_a_server_that_does_not_answer_fails_with_the_plain_sentence(store):
    reg = Registry()
    cfg = _cfg("dead", transport="http", command=None, args=[], url="http://127.0.0.1:9/mcp",
               startup_timeout_seconds=1, max_restarts=0)
    sup = Supervisor(cfg, store, reg, ping_seconds=0.5)
    async with anyio.create_task_group() as tg:
        await sup.start(tg)
        skill = sup.skills["dead"]
        await until(lambda: skill.status == "dead")
        tg.cancel_scope.cancel()
    assert skill.error == ("The dead server did not answer at 127.0.0.1:9. Start it, or fix "
                           "the address.")


def test_an_mcp_argument_named_like_a_python_keyword_keeps_its_name():
    from celine.turns.tools import _takes_any

    async def mcp(ctx, **args): ...
    async def own(ctx, in_: str): ...
    assert _takes_any(mcp) and not _takes_any(own)
