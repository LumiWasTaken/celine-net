"""Sub-agents and missions over HTTP: the overview, start and stop, the snapshot's
who-is-this, and the global events a client keeps its lists with."""
from __future__ import annotations

from chat_fixtures import until, user_says

import anyio
import httpx
import pytest
from fastapi import FastAPI

from celine.boot import load
from celine.conversations import api as conversations_api, store as conversations
from celine.core.bus import EventBus
from celine.core.services import Services
from celine.core.store import Store
from celine.errors import install_error_handler
from celine.intercom import Intercom
from celine.missions import api as missions_api
from celine.skills import Registry
from celine.sub_agents import api as agents_api
from celine.sub_agents.skill import SubAgents
from celine.turns.dispatcher import Dispatcher

pytestmark = pytest.mark.anyio


async def test_agents_and_missions_are_managed_over_http_with_events(db_path):
    async with anyio.create_task_group() as tg:
        bus = EventBus()
        store = Store(db_path, bus=bus)
        await store.start(tg)
        cfg = load()
        tx, rx = bus.subscribe("global")
        seen: list[dict] = []

        async def collect():
            async with rx:
                async for ev in rx:
                    seen.append(ev)
        tg.start_soon(collect)

        reg = Registry()
        skill = SubAgents(cfg)
        reg.add(skill)
        dispatcher = Dispatcher(store, cfg, bus, lambda: None, reg)
        intercom = Intercom(store, lambda: None)
        dispatcher.intercom = intercom
        services = Services(cfg=cfg, store=store, bus=bus, registry=reg,
                            dispatcher=dispatcher, intercom=intercom)
        skill.app = services
        app = FastAPI()
        app.state.services = Services()
        install_error_handler(app)
        for r in (agents_api.router, missions_api.router, conversations_api.router):
            app.include_router(r)
        app.state.services.store, app.state.services.cfg, app.state.services.bus = store, cfg, bus
        app.state.services.intercom, app.state.services.dispatcher, app.state.services = \
            intercom, dispatcher, services

        parent = await conversations.create(store, "p")
        await user_says(store, parent, "hi")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            r = await c.post(f"/api/conversations/{parent}/sub-agents",
                             json={"brief": "find the cars", "name": "car scout"})
            assert r.status_code == 200, r.text
            sub = r.json()["conversation_id"]
            r = await c.post(f"/api/conversations/{parent}/sub-agents",
                             json={"brief": "again", "name": "car scout"})
            assert r.status_code == 409 and "still working" in r.json()["error"]["message"]
            chain = [parent]
            for n in ("one", "two", "three"):
                chain.append(await conversations.create(store, n, agent_of=chain[-1],
                                                        agent_name=n))
            deep = chain[-1]
            r = await c.post(f"/api/conversations/{deep}/sub-agents",
                             json={"brief": "nested", "name": "inner scout"})
            assert r.status_code == 409 and r.json()["error"]["message"] == cfg.substitute(
                "Sub-agents go at most 3 levels below {{name}}, and this conversation is "
                "3 below: it starts none."), "depth 3"
            tree = (await c.get(f"/api/conversations/{deep}/sub-agents")).json()
            assert tree["top"] == parent, "from anywhere in it: the whole tree"
            assert [(a["name"], a["depth"], a["parent"], a["state"], a["link"])
                    for a in tree["agents"]] == [
                ("car scout", 1, parent, "working", f"/chat/{sub}"),
                ("one", 1, parent, "done", f"/chat/{chain[1]}"),
                ("two", 2, chain[1], "done", f"/chat/{chain[2]}"),
                ("three", 3, chain[2], "done", f"/chat/{chain[3]}")]
            assert {(a["prompt_tokens"], a["seconds"]) for a in tree["agents"][1:]} == {(0, 0)}
            assert (await c.get("/api/conversations/nope/sub-agents")).status_code == 404
            await store.write(lambda c: c.execute(
                "DELETE FROM conversations WHERE agent_of IS NOT NULL AND id <> ?", (sub,)))

            got = (await c.get(f"/api/sub-agents?conversation={parent}")).json()["agents"]
            assert [(a["id"], a["name"], a["parent"], a["working"], a["access"])
                    for a in got] == [(sub, "car scout", parent, True, "read")]
            assert (await c.get("/api/sub-agents")).json()["agents"][0]["id"] == sub

            snap = (await c.get(f"/api/conversations/{sub}")).json()["conversation"]
            assert (snap["agent_of"], snap["agent_name"], snap["access"]) == \
                (parent, "car scout", "read")
            snap = (await c.get(f"/api/conversations/{parent}")).json()["conversation"]
            assert [a["id"] for a in snap["agents"]] == [sub] and snap["mission"] is None

            assert (await c.post(f"/api/sub-agents/{sub}/stop")).json() == {"stopped": True}
            assert (await c.post("/api/sub-agents/nope/stop")).status_code == 404
            got = (await c.get(f"/api/sub-agents?conversation={parent}")).json()["agents"]
            assert got[0]["working"] is False
            r = await c.post(f"/api/conversations/{parent}/sub-agents",
                             json={"brief": "x", "name": "car scout", "access": "work"})
            assert r.json()["error"]["message"] == (
                "car scout exists with read access; start it under another name or leave "
                "access, folder, model, cli and device out.")
            r = (await c.post(f"/api/conversations/{parent}/sub-agents",
                              json={"brief": "and the blue ones", "name": "Car Scout"})).json()
            assert r == {"conversation_id": sub, "resumed": True}, "done: it takes the brief"
            briefs = await store.read(lambda c_: [x[0] for x in c_.execute(
                "SELECT text FROM intercom WHERE kind='brief' AND to_addr=?",
                (f"agent:{sub}",))])
            assert briefs == ["find the cars", "and the blue ones"]

            r = await c.post("/api/missions", json={"name": "RSS", "brief": "fix the feed"})
            mission = r.json()["conversation_id"]
            snap = (await c.get(f"/api/conversations/{mission}")).json()["conversation"]
            assert snap["kind"] == "mission"
            await c.patch(f"/api/missions/{mission}", json={"brief": "fix the feed today"})
            rows = (await c.get("/api/missions")).json()["missions"]
            assert rows[-1]["brief"] == "fix the feed today"
            assert await store.read(lambda c_: [r[0] for r in c_.execute(
                "SELECT text FROM text_versions WHERE of=? ORDER BY id",
                (f"mission:{mission}:brief",))]) == ["fix the feed", "fix the feed today"], \
                "the brief and each change in its history"
            assert [(m["conversation_id"], m["name"], m["status"]) for m in rows][-1] == \
                (mission, "RSS", "active")
            assert (await c.get("/api/missions?status=done")).json()["missions"] == []
            snap = (await c.get(f"/api/conversations/{mission}")).json()["conversation"]
            assert snap["mission"]["name"] == "RSS" and snap["mission"]["status"] == "active"
            await c.patch(f"/api/conversations/{parent}", json={"pinned": True})
            assert (await c.delete(f"/api/conversations/{parent}")).status_code == 409, \
                "the parent's own turn is still queued"
            await store.write(lambda c_: c_.execute(
                "UPDATE turns SET state='done' WHERE conversation_id IN (?, ?)",
                (parent, sub)))
            # without a conversation only the live ones: an archived parent takes its
            # sub-agents out of the overview, its own list still has them
            assert sub in [a["id"] for a in (await c.get("/api/sub-agents")).json()["agents"]]
            await conversations.archive(store, parent)
            assert sub not in [a["id"] for a in (await c.get("/api/sub-agents")).json()["agents"]]
            assert sub in [a["id"] for a in (await c.get(
                f"/api/sub-agents?conversation={parent}")).json()["agents"]]
            await conversations.archive(store, parent, False)
            assert (await c.delete(f"/api/conversations/{parent}")).status_code == 200

        await until(lambda: sum(e["type"] == "conversation.deleted" for e in seen) == 2)
        types = [e["type"] for e in seen]
        assert types.count("conversation.created") >= 3
        assert "agent.state" in types and "mission.state" in types
        assert any(e["type"] == "conversation.pinned" and e["id"] == parent and e["pinned"] is True
                   for e in seen)
        assert [e["id"] for e in seen if e["type"] == "conversation.deleted"] == [parent, sub]
        started = next(e for e in seen if e["type"] == "agent.state")
        assert started["id"] == sub and started["working"] is True
        assert started["access"] == "read"
        bus.unsubscribe("global", tx)
        await tx.aclose()
        tg.cancel_scope.cancel()
