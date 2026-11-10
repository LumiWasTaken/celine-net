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
from parts_fixtures import ROOM, joined

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


async def test_a_deleted_conversation_leaves_the_agents_page(store):
    cid = await conversations.create(store)
    aid = (await _write(store, cid, title="Plan", content="<p>v1</p>")).display["artifact"]["id"]
    again = await _write(store, cid, title="Plan", content="<p>new</p>")
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


async def test_a_page_is_stored_as_the_agent_wrote_it(store):
    cid = await conversations.create(store)
    text = "# Prices\n\nbolts are cheap [[web:3]] and [web:9]【2†source】"
    aid = (await _write(store, cid, title="Prices", content=text, kind="markdown")
           ).display["artifact"]["id"]
    async with _client(_app(store)) as c:
        source = await c.get(f"/api/artifacts/{aid}/versions/1/source")
    assert source.text == text


async def test_a_write_reports_the_bytes_stored_for_its_version(store):
    from celine.artifacts import store as artifacts
    cid = await conversations.create(store)
    page_v1 = "<!DOCTYPE html>\n" + "<p>Grüße, ok</p>\n" * 3500        # about 60 KB
    page_v2 = page_v1.replace("ok", "fine")
    first = await _write(store, cid, title="Big", content=page_v1)
    aid = first.display["artifact"]["id"]
    second = await _write(store, cid, content=page_v2, id=aid)
    stored = [v["bytes"] for v in (await artifacts.get(store, aid))["versions"]]
    said = [int(re.search(r"version \d+, ([\d,]+) bytes", r.content)[1].replace(",", ""))
            for r in (first, second)]
    assert said == stored == [len(page_v1.encode()), len(page_v2.encode())]


async def test_an_artifact_id_and_title_match_exactly(store):
    cid = await conversations.create(store)
    aid = (await _write(store, cid, title="Plan", content="<p>v1</p>")).display["artifact"]["id"]
    ctx = ToolContext(cid, "t", "call-1", store, load(), None)  # type: ignore[arg-type]
    out = await Artifacts(load()).read(ctx, id=aid.upper())
    assert out.is_error and out.content.startswith(f"There is no artifact {aid.upper()}")
    assert not (await _write(store, cid, title="plan", content="<p>other</p>")).is_error


async def test_a_new_version_keeps_its_title_and_a_new_artifact_needs_one(store):
    from celine.skills import Registry
    reg = Registry()
    reg.add(Artifacts(load()))
    assert reg.get("artifacts__write").schema["required"] == ["content"]
    cid = await conversations.create(store)
    assert (await _write(store, cid, content="<p>1</p>")).is_error
    aid = (await _write(store, cid, title="Game", content="<p>1</p>")).display["artifact"]["id"]
    again = await _write(store, cid, id=aid, content="<p>2</p>")
    assert not again.is_error and again.display["artifact"]["title"] == "Game"


async def test_a_long_page_reads_in_parts_that_join_to_the_page(store):
    import random
    from dataclasses import replace
    rnd = random.Random(7)
    words = ["bolt", "nut", "washer", "price", "<td>", "</tr>", "€3.50", "{x: 1}"]
    content = "\n".join(
        " ".join(rnd.choice(words) for _ in range(rnd.randint(0, 14))) + ("\n" if i % 9 == 0 else "")
        for i in range(4000))
    assert len(content.encode()) > 60_000
    cid = await conversations.create(store)
    aid = (await _write(store, cid, title="Big", content=content)).display["artifact"]["id"]
    ctx = replace(ToolContext(cid, "t", "call-1", store, load(), None),  # type: ignore[arg-type]
                  max_output_tokens=ROOM)

    async def read(k):
        return (await Artifacts(load()).read(ctx, id=aid, part=k)).content
    call = f"artifacts__read(id=\"{aid}\", version=1"
    whole, n = await joined(read, call)
    assert n > 1 and whole == content, "the parts joined are the page"
    first = await read(None)
    assert re.match(r'"Big" \(\w+\), version 1 of 1[.:]', first)
    assert first.endswith(f"\n\nlater: {call}, part=2)")
    last = await read(99)
    assert f"\npart {n} of {n}\n" in last and last.endswith(f"\n\nearlier: {call}, part={n - 1})")
