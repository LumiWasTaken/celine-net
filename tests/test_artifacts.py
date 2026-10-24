"""The agent's artifacts: a page always comes sandboxed by its own headers, the frame's sandbox
never grows, and its pages outlive the conversation they were made in."""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from celine.conversations import store as conversations
from celine.core.services import Services
from celine.errors import install_error_handler
from celine.api.headers import SecurityHeaders
from celine.artifacts import api as api_mod
from celine.artifacts import page
from celine.boot import load
from celine.core.bus import EventBus
from celine.skills import ToolContext
from celine.skills.artifacts import Artifacts

pytestmark = pytest.mark.anyio

WEB = Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "artifacts.ts"
def _app(store) -> FastAPI:
    app = FastAPI()
    app.state.services = Services()
    install_error_handler(app)
    app.add_middleware(SecurityHeaders)
    app.include_router(api_mod.router)
    app.state.services.store, app.state.services.cfg, app.state.services.bus = store, load(), EventBus()
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"origin": "http://127.0.0.1:8766"})


async def _write(store, cid: str, **args):
    ctx = ToolContext(cid, "t", "call-1", store, load(), None)  # type: ignore[arg-type]
    return await Artifacts(load()).write(ctx, **args)


async def test_no_page_answers_without_its_sandbox(store):
    cid = await conversations.create(store)
    aid = (await _write(store, cid, title="Game", content="<canvas></canvas>")
           ).display["artifact"]["id"]
    doc = (await _write(store, cid, title="Notes", content="# <b>Hi</b>", kind="markdown")
           ).display["artifact"]["id"]
    async with _client(_app(store)) as c:
        pages = [await c.get(f"/api/artifacts/{aid}/versions/1"),
                 await c.get(f"/api/artifacts/{aid}/versions/1/source"),
                 await c.get(f"/api/artifacts/{doc}/versions/1")]
    assert pages[0].text == page.HEAD + "<canvas></canvas>"
    for r in pages:
        assert r.headers["content-security-policy"].startswith(f"sandbox {page.SANDBOX};")
        assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "allow-same-origin" not in str(r.headers)
    # what it wrote, and a document, come as text: nothing in them runs
    assert [r.headers["content-type"] for r in pages[1:]] == ["text/plain; charset=utf-8"] * 2
    # the frame's attribute is the header's sandbox, nothing more
    frame = re.search(r"export const SANDBOX = '([^']*)'", WEB.read_text())[1]
    assert frame == page.SANDBOX == "allow-scripts allow-forms"


async def test_a_deleted_conversation_leaves_her_page(store):
    cid = await conversations.create(store)
    aid = (await _write(store, cid, title="Plan", content="<p>v1</p>")).display["artifact"]["id"]
    again = await _write(store, cid, title="plan ", content="<p>new</p>")
    assert again.is_error and aid in again.content, "a second page with the title is refused"
    await _write(store, cid, title="", content="<p>v2</p>", id=aid, note="shorter")
    assert await conversations.delete(store, cid)
    async with _client(_app(store)) as c:
        meta = (await c.get(f"/api/artifacts/{aid}")).json()
        v1 = await c.get(f"/api/artifacts/{aid}/versions/1")
    assert [(v["n"], v["title"], v["note"]) for v in meta["versions"]] == [
        (1, "Plan", ""), (2, "Plan", "shorter")]
    assert meta["conversation_id"] == cid
    assert v1.text.endswith("<p>v1</p>")
