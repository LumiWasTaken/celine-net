"""The intercom: the delivery rule (intercom/deliver.py), a message that arrives while a
turn runs joins it at its next step; threads, tokens, announcements and the Waiting count;
intercom__send(wait=true) holds its turn until the user answers."""
from __future__ import annotations

from types import SimpleNamespace

from chat_fixtures import user_says

import anyio
import pytest

from celine import boot
from celine.conversations import store as conversations, view
from celine.core.services import Services
from celine.backends.base import Done, TextDelta, ToolCallDelta
from fake_backend import FakeBackend
from celine.core.bus import EventBus
from celine.intercom import Intercom
from celine.intercom.skill import IntercomSkill
from celine.skills import Registry, ToolContext, ToolResult, tool
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from celine.turns import store as turns

from jobs_fixtures import cfg  # noqa: F401  (the fixture)

pytestmark = pytest.mark.anyio


class Door:
    """A tool during which the user writes again."""
    name, description = "door", "rings"

    @tool()
    async def ring(self, ctx) -> ToolResult:
        """Rings."""
        await user_says(ctx.store, ctx.conversation_id, "and the blue one too")
        return ToolResult(content="rang")


class Twice(FakeBackend):
    """Calls door__ring in its first step, answers in its second."""

    async def stream(self, messages, tools):
        self.calls.append(messages)
        if len(self.calls) == 1:
            yield ToolCallDelta(index=0, id="d1", name="door__ring", arguments="{}")
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("both")
            yield Done(reason="stop")


async def _turns(store, cid):
    return await store.read(lambda c: [tuple(r) for r in c.execute(
        "SELECT origin, state FROM turns WHERE conversation_id=? ORDER BY created_at",
        (cid,))])


async def test_a_message_during_a_turn_joins_it_at_the_next_step(store, cfg):
    reg = Registry()
    reg.add(Door())
    cid = await conversations.create(store)
    await user_says(store, cid, "find the red car")
    turn = await turns.claim(store, "w")
    backend = Twice()
    await run_turn(turn, store, cfg, backend, EventBus(), Token(turn["id"]), reg)

    snap = await view.snapshot(store, cid)
    msgs = snap["messages"]
    assert [(m["role"], m["author"]) for m in msgs] == [
        ("user", "user"), ("assistant", "model"), ("tool", "model"),
        ("user", "user"), ("assistant", "model")], "under its own tail, one line"
    assert all(m["siblings"] == 1 for m in msgs)
    assert msgs[3]["content"] == "and the blue one too" and msgs[3]["turn_id"] == turn["id"]
    assert msgs[3]["parent_id"] == msgs[2]["id"]
    assert backend.calls[1][-1] == {"role": "user", "content": backend.calls[1][-1]["content"],
                                    "origin": [{"conversation": cid, "turn": turn["id"],
                                                "message": msgs[3]["id"]}]}
    assert backend.calls[1][-1]["content"].endswith("and the blue one too")
    assert await _turns(store, cid) == [("user", "done")], "no second turn"
    assert await store.read(lambda c: c.execute(
        "SELECT state, turn_id FROM intercom_deliveries ORDER BY intercom_id DESC LIMIT 1"
    ).fetchone()[:]) == ("delivered", turn["id"])


async def _family(store):
    parent = await conversations.create(store)
    subs = [await conversations.create(store, title=n, agent_of=parent, agent_name=n,
                                            access="read")
            for n in ("scout one", "scout two", "scout three")]
    return parent, subs


async def _queued(store, cid):
    return await store.read(lambda c: c.execute(
        "SELECT count(*) FROM turns WHERE conversation_id=? AND state='queued'",
        (cid,)).fetchone()[0])


async def test_a_sub_agent_asks_its_parent_and_the_answer_wakes_it_in_the_thread(store):
    from celine.intercom import Intercom
    ic = Intercom(store)
    parent, (sub, *_) = await _family(store)
    asked = await ic.send(f"agent:{sub}", "parent", "Which folder?", subject="folder")
    assert asked["turn_id"] and await _queued(store, parent) == 1, "the parent wakes"

    answer = await ic.send(f"agent:{parent}", "", "agents/cars", reply_to=asked["intercom_id"])
    assert answer["turn_id"] and await _queued(store, sub) == 1, "the sub-agent wakes"
    row = await store.read(lambda c: c.execute(
        "SELECT to_addr, thread, subject FROM intercom WHERE id=?",
        (answer["intercom_id"],)).fetchone())
    assert tuple(row) == (f"agent:{sub}", asked["intercom_id"], "folder")


async def test_the_users_reply_marks_the_thread_read(store):
    """A message waits for the user until they reply (or dismiss it), not until they
    open the thread."""
    import json

    from celine.intercom import Intercom, store as rows
    ic = Intercom(store)
    parent, _ = await _family(store)
    asked = await ic.send("user", f"agent:{parent}", "which one?")
    a = await ic.send(f"agent:{parent}", "", "the blue", reply_to=asked["intercom_id"])
    b = await ic.send(f"agent:{parent}", "", "or the red", reply_to=asked["intercom_id"])
    other = await ic.send(f"agent:{parent}", "user", "elsewhere")
    assert await store.read(lambda c: rows.waiting(c, "user")) == 3
    gid = await store.read(lambda c: c.execute("SELECT max(id) FROM global_events"
                                               ).fetchone()[0])
    await ic.send("user", "", "blue", reply_to=b["intercom_id"])
    assert await store.read(lambda c: rows.waiting(c, "user")) == 1, "the other thread waits"
    changed = await store.read(lambda c: [json.loads(r[0])["intercom_id"] for r in c.execute(
        "SELECT data FROM global_events WHERE id > ? AND type='intercom.state'", (gid,))])
    assert {a["intercom_id"], b["intercom_id"]} <= set(changed)
    assert other["intercom_id"] not in changed


async def test_an_announcement_shows_in_a_turn_that_starts_after_it(store, cfg):
    from datetime import datetime, timedelta, timezone

    from celine.core.ids import iso
    from celine.intercom import Intercom
    from celine.intercom.skill import IntercomSkill
    from celine.turns.prompt import system_blocks
    working = await conversations.create(store)
    await user_says(store, working, "busy")          # a queued turn: working
    until = iso(datetime.now(timezone.utc) + timedelta(hours=1))
    await Intercom(store).send("system:updater", "all", "the server restarts soon.",
                               kind="notice", until_at=until)
    later = await conversations.create(store)
    reg = Registry()
    reg.add(IntercomSkill(cfg))
    from celine.turns import loads
    carried = await loads.select(store, cfg, reg, later, None)
    system = "\n\n".join(t for _, t in await system_blocks(cfg, reg, store, later, "", carried))
    assert "Announcements to everyone working: #" in system
    assert "from updater, until" in system and "the server restarts soon." in system


async def test_the_stream_replays_from_after_and_then_follows_a_new_message(store):
    from types import SimpleNamespace
    from celine.intercom import Intercom
    from celine.intercom.api import stream

    ic = Intercom(store)
    cid = await conversations.create(store)
    first = (await ic.send("user", f"agent:{cid}", "one"))["intercom_id"]
    await ic.send("user", f"agent:{cid}", "two")
    req = SimpleNamespace(headers={}, app=SimpleNamespace(state=SimpleNamespace(
        services=Services(store=store, intercom=ic))))
    events = (await stream(req, after=first)).body_iterator
    assert '"text": "two"' in await events.__anext__()
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: ic.send("agent:" + cid, "user", "three"))
        with anyio.fail_after(2):
            got = await events.__anext__()
    assert got.startswith("id: 3\n") and '"text": "three"' in got
    await events.aclose()


def _gated_app(store):
    """The real app with the login on, the gate in front, without its lifespan."""
    from celine.__main__ import build_app
    from celine.intercom import Intercom
    cfg = boot.load()
    cfg.server.update({"login": "on"})
    app = build_app(cfg, dev=False)
    app.state.services = Services(cfg=cfg, store=store, intercom=Intercom(store))
    return app


async def test_a_named_token_does_not_act_as_the_user(store):
    """A token posts and reads; it does not read the user's messages for them, carry a
    thread into a chat, or cancel a message it did not send."""
    import httpx
    from celine.intercom import tokens
    token = await store.write(lambda c: tokens.make(c, "cli"))
    other = await store.write(lambda c: tokens.make(c, "nightly"))
    app = _gated_app(store)
    auth = lambda t: {"authorization": f"Bearer {t}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost:8766") as http:
        mine = (await http.post("/api/intercom", headers=auth(token),
                                json={"to": "user", "text": "later", "in": "30 min"})).json()
        theirs = (await http.post("/api/intercom", headers=auth(other),
                                  json={"to": "user", "text": "later", "in": "30 min"})).json()
        now_ = (await http.post("/api/intercom", headers=auth(token),
                                json={"to": "user", "text": "now"})).json()
        tid = await store.read(lambda c: c.execute(
            "SELECT COALESCE(thread, id) FROM intercom WHERE id=?",
            (now_["intercom_id"],)).fetchone()[0])
        r = await http.post("/api/intercom/read?all=1", headers=auth(token))
        assert r.status_code == 403
        r = await http.post("/api/intercom/read", headers=auth(token),
                            json={"ids": [now_["intercom_id"]]})
        assert r.status_code == 403
        r = await http.post(f"/api/intercom/threads/{tid}/conversations", headers=auth(token))
        assert r.status_code == 403
        r = await http.post(f"/api/intercom/{theirs['intercom_id']}/stop", headers=auth(token))
        assert r.status_code == 404
        r = await http.post(f"/api/intercom/{mine['intercom_id']}/stop", headers=auth(token))
        assert r.status_code == 200, "its own scheduled message it may take back"
        r = await http.get("/api/intercom", headers=auth(token))
        assert r.status_code == 200, "it still reads"
    assert await store.read(lambda c: c.execute(
        "SELECT cancelled_at IS NULL FROM intercom WHERE id=?",
        (theirs["intercom_id"],)).fetchone()[0])


async def test_a_named_token_sends_on_the_intercom_only_a_revoked_one_gets_404(store):
    """A token made in Settings is for a host script on the intercom: it sends there as
    `system:<name>` and reaches nothing else. A revoked or wrong one is turned away."""
    import httpx
    from celine.intercom import tokens
    cid = await conversations.create(store, "mine")
    await user_says(store, cid, "busy")              # a queued turn: working
    token = await store.write(lambda c: tokens.make(c, "cli"))
    gone = await store.write(lambda c: tokens.make(c, "nightly"))
    tid = await store.read(lambda c: c.execute(
        "SELECT id FROM intercom_tokens WHERE name='nightly'").fetchone()[0])
    await store.write(lambda c: tokens.revoke(c, tid))
    app = _gated_app(store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost:8766") as http:
        for method, path in (("GET", "/api/conversations"), ("GET", "/openapi.json"),
                             ("GET", "/api/auth/credentials"),
                             ("POST", "/api/auth/credentials"),
                             ("DELETE", "/api/auth/credentials/1"),
                             ("POST", "/api/auth/recovery-codes"),
                             ("GET", "/api/tokens"), ("POST", "/api/tokens"),
                             ("POST", "/api/tokens/1/revoke")):
            r = await http.request(method, path, headers={"authorization": f"Bearer {token}"})
            assert r.status_code == 404, (method, path)
        r = await http.post("/api/intercom", headers={"authorization": f"Bearer {token}"},
                            json={"to": "all", "text": "restarts at 05:00.", "until": "05:00"})
        assert r.status_code == 200, r.text
        iid = r.json()["intercom_id"]
        for headers in ({"authorization": f"Bearer {gone}"},
                        {"authorization": "Bearer wrong"}, {}):
            r = await http.post("/api/intercom", headers=headers,
                                json={"to": "all", "text": "hi"})
            assert r.status_code == 404
            assert r.json() == {"error": {"code": "not_found", "message": "/api/intercom"}}
    row = await store.read(lambda c: c.execute(
        "SELECT from_addr, until_at FROM intercom WHERE id=?", (iid,)).fetchone())
    assert row["from_addr"] == "system:cli" and row["until_at"], "a token sends as system"
    assert await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT conversation_id FROM intercom_deliveries WHERE intercom_id=?", (iid,))]) == [cid]
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM intercom WHERE from_addr LIKE 'system:%'").fetchone()[0]) == 1, \
        "the refused ones sent nothing"


async def test_her_threads_group_by_who_and_chat_and_job_results_are_no_threads(store, cfg):
    """Celine's note: one-message threads from chat traffic and job results buried the
    threads that matter."""
    import httpx
    from fastapi import FastAPI
    from celine.errors import install_error_handler
    from celine.intercom import api as intercom_api, store as rows
    from celine.intercom.skill import IntercomSkill
    from celine.skills import ToolContext
    parent, (scout, *_) = await _family(store)

    def put(frm, to, kind, text, **kw):
        return store.write(lambda c: rows.insert(c, frm, to, kind, text, **kw))
    for t in ("hi", "and this", "one more"):                    # the user's plain chat
        await put("user", f"agent:{parent}", "message", t)
    for _ in range(2):
        job = await put(f"agent:{parent}", f"agent:{parent}", "job", "made it")
    for n in range(4):
        await put(f"agent:{scout}", f"agent:{parent}", "report", f"found {n}", subject=f"s{n}")
    await put(f"agent:{parent}", "user", "message", "done?")

    skill, ctx = IntercomSkill(cfg), ToolContext(parent, "t", "k", store, cfg, None)
    out = (await skill.read(ctx)).content.splitlines()
    assert out[0].startswith("with ") and out[0].endswith(": 1 thread")    # the user
    assert out[2] == "with scout one: 4 threads, 4 waiting"
    assert [ln.split(":")[0] for ln in out[3:6]] == ['  #9 "s3"', '  #8 "s2"', '  #7 "s1"']
    assert out[6] == '  and 1 more: intercom__read(with="scout one").'
    assert out[7] == f'2 job results, newest #{job}: intercom__read(kind="job").'
    assert len(out) == 8 and not any("hi" in ln for ln in out)
    assert len((await skill.read(ctx, with_="scout one")).content.splitlines()) == 5
    jobs = (await skill.read(ctx, kind="job")).content
    assert jobs.count("made it") == 2 and "job results" not in jobs
    assert "wait for you" not in (await skill.prompt_fragment(ctx) or "")

    app = FastAPI()
    app.state.services = Services()
    install_error_handler(app)
    app.include_router(intercom_api.router)
    app.state.services.store = store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as http:
        page = (await http.get("/api/intercom/threads")).json()     # to you
        assert [(g["name"], g["threads"]) for g in page["groups"]] == [("Celine", 1)]
        assert page["jobs"] == {"count": 0, "newest": None} and page["next"] is None
        first = (await http.get("/api/intercom/threads?with=all&limit=1")).json()
        assert [g["name"] for g in first["groups"]] == ["Celine"] and first["next"]
        second = (await http.get(f"/api/intercom/threads?with=all&limit=1&cursor="
                                 f"{first['next']}")).json()
        assert [(g["name"], len(g["items"]), g["more"]) for g in second["groups"]] == \
            [("scout one, Celine", 3, 1)]
        one = (await http.get("/api/intercom/threads?with=scout one")).json()
        assert [len(g["items"]) for g in one["groups"]] == [4]
        assert (await http.get("/api/intercom/threads?kind=nope")).status_code == 400


async def test_two_counterparts_with_one_name_show_their_addresses(store, cfg):
    from celine.intercom import store as rows
    from celine.intercom.skill import IntercomSkill
    from celine.skills import ToolContext
    me, a, b = [await conversations.create(store) for _ in range(3)]
    for frm in (a, b):
        await store.write(lambda c, frm=frm: rows.insert(
            c, f"agent:{frm}", f"agent:{me}", "report", "hello", subject="s"))
    out = (await IntercomSkill(cfg).read(ToolContext(me, "t", "k", store, cfg, None))).content
    assert f"with Celine (agent:{a}): 1 thread" in out and f"(agent:{b})" in out


async def test_the_badge_counts_what_waiting_lists_and_dismiss_all_reads_it(store):
    """The rail's badge (/api/changes `unread`) and the Waiting list are one count: a
    note to yourself waits and is listed, a job result is neither."""
    import httpx
    from fastapi import FastAPI
    from celine.intercom import Intercom, api as intercom_api, store as rows
    ic = Intercom(store)
    cid = await conversations.create(store)
    await ic.send("user", "user", "drink water")
    await ic.send(f"agent:{cid}", "user", "backup failed")
    await store.write(lambda c: rows.insert(c, f"agent:{cid}", "user", "job", "made it"))
    found = await store.read(lambda c: rows.threads(c, "user", unread=True, reader="user",
                                                    received=True))
    assert len(found) == 2 and "Celine" in {t["name"] for t in found}
    assert await store.read(lambda c: rows.waiting(c, "user")) == \
        sum(t["unread"] for t in found) == 2

    app = FastAPI()
    app.state.services = Services()
    app.include_router(intercom_api.router)
    app.state.services.store = store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as http:
        assert len((await http.post("/api/intercom/read?all=1")).json()["read"]) == 2
    assert await store.read(lambda c: rows.waiting(c, "user")) == 0


async def test_a_background_tasks_threads_stay_inside_it(store):
    """A partner's lines into a run, a trigger's fire into a background task and its
    reply in those threads are never listed; its own message to the user is."""
    from celine.intercom import Intercom, store as rows
    ic = Intercom(store)
    session = await conversations.for_trigger(store, "run:0123:1", "Reflection 2026-01-10")
    bg = await conversations.for_trigger(store, "trigger:mail.new", "mail.new")
    line = await ic.send("system:mentor", f"agent:{session}", "Why that?")
    await ic.send(f"agent:{session}", "all", "Because.", reply_to=line["intercom_id"])
    await ic.send("system:mail", f"agent:{bg}", "New mail.", kind="trigger")
    mine = await ic.send(f"agent:{session}", "user", "The groceries chat went badly.")
    for addr, received in ((None, False), ("user", True)):
        found = await store.read(lambda c: rows.threads(c, addr, reader="user",
                                                        received=received))
        assert [t["thread"] for t in found] == [mine["intercom_id"]], addr
    assert await store.read(lambda c: rows.threads(c, None, kind="trigger")) == []


async def test_her_answer_to_a_host_script_reaches_the_user(store):
    from celine.intercom import Intercom, tokens
    await store.write(lambda c: tokens.make(c, "server-updates"))
    cid = await conversations.create(store)
    ic = Intercom(store)
    first = await ic.send("system:server-updates", f"agent:{cid}", "REA is set up.")
    for to, reply_to in (("", first["intercom_id"]), ("system:server-updates", None),
                         ("server-updates", None)):
        sent = await ic.send(f"agent:{cid}", to, "thanks, it works.", reply_to=reply_to)
        row = await store.read(lambda c: c.execute(
            "SELECT to_addr FROM intercom WHERE id=?", (sent["intercom_id"],)).fetchone())
        assert row["to_addr"] == "user", (to, reply_to)


async def test_wait_holds_until_the_user_answers(store, cfg):
    ic = Intercom(store)
    skill = IntercomSkill(cfg)
    skill.app = SimpleNamespace(intercom=ic)
    cid = await conversations.create(store)
    shown = anyio.Event()
    notes = []

    async def emit(note, **_):
        notes.append(note)
        shown.set()
    ctx = ToolContext(cid, "t", "k", store, cfg, emit)
    out = {}

    async def ask():
        out["r"] = await skill.send(ctx, "ready for the next step?", "user", wait=True)
    async with anyio.create_task_group() as tg:
        tg.start_soon(ask)
        await shown.wait()
        assert notes == [f"Waiting for {cfg.user}"]
        assert "r" not in out, "its turn holds"
        asked = await store.read(lambda c: c.execute(
            "SELECT max(id) FROM intercom WHERE from_addr=?", (f"agent:{cid}",)).fetchone()[0])
        reply = await ic.send("user", "", "yes, go", reply_to=asked)
    assert out["r"].content.endswith(f"{cfg.user} answered (#{reply['intercom_id']}).")


async def test_a_reply_wakes_a_conversation_whose_turn_was_stopped(store):
    ic = Intercom(store)
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "fetch the logs")
    await turns.claim(store, "w")
    await turns.transition(store, tid, cid, "aborted")
    asked = await ic.send(f"agent:{cid}", "user", "stopped halfway, go on?")
    reply = await ic.send("user", "", "yes", reply_to=asked["intercom_id"])
    assert reply["turn_id"] and reply["turn_id"] != tid, "the reply starts its next turn"
