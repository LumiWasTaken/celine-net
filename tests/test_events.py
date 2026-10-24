"""GET /api/events: every kind from the rows that exist, newest first, the Type, Date and
state filters, Bastet's filters, paging; and `events.added` on the global feed when one lands."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from celine import events
from celine.conversations import store as conversations, tree
from celine.core.services import Services
from celine.errors import install_error_handler
from celine.intercom.store import insert

CFG = SimpleNamespace(user="Joe", name="Celine", timezone="Europe/Berlin", schedules={})


def _at(hh_mm: str, day: str = "2026-10-01") -> str:
    return f"{day}T{hh_mm}:00.000+00:00"


async def _rows(store) -> tuple[str, str]:
    cid = await conversations.create(store, title="find a job")
    other = await conversations.create(store, title="other")

    def fn(c):
        m = tree.append(c, store, cid, None, "assistant", "on it", "model", turn_id="t1")
        c.execute("INSERT INTO turns(id, conversation_id, root_message_id, state, model, "
                  "created_at, finished_at) VALUES('t1', ?, ?, 'done', 'chat-model', ?, ?)",
                  (cid, m["id"], _at("10:00"), "2026-10-01T10:00:12.000+00:00"))
        c.execute("INSERT INTO turns(id, conversation_id, state, reason, created_at) "
                  "VALUES('t2', ?, 'failed', 'the model server did not answer', ?)",
                  (cid, _at("11:00")))
        for id_, name, args, state, at in (
                ("a", "shell__run", '{"command": "ls -la\\nmore"}', "ok", _at("10:01")),
                ("b", "browser__open", '{"url": "https://x.org"}', "error", _at("10:02")),
                ("n", "notebook__write", '{"path": "people/noor.md", "content": "x", '
                                         '"summary": "who Noor is"}', "ok", _at("10:03")),
                ("j", "image_generator__generate", '{"prompt": "a harbour"}', "detached", _at("10:04"))):
            c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state, started_at) "
                      "VALUES(?,?,?,?,?,?)", (id_, m["id"], name, args, state, at))
        c.execute("INSERT INTO jobs(id, conversation_id, origin_turn_id, tool_call_id, tool_name, "
                  "state, created_at, finished_at) VALUES('J', ?, 't1', 'j', "
                  "'image_generator__generate', 'done', ?, ?)",
                  (cid, _at("10:05"), _at("10:07")))
        insert(c, f"agent:{cid}", "user", "message", "I found three.\nmore", store=store,
               created_at=_at("10:06"))
        insert(c, "user", f"agent:{cid}", "message", "thanks", store=store,
               created_at=_at("10:08"))      # the user's: not its own going out
        # stored as "agent:<other>, user": the user second is its own to the user too
        insert(c, f"agent:{cid}", f"agent:{other}, user", "message", "both of you",
               store=store, created_at=_at("10:09"))
        insert(c, f"agent:{cid}", f"agent:{other}", "message", "only you", store=store,
               created_at=_at("10:10"))     # not to the user
        for rid, state, at in (("r1", "failed", "2026-09-30T23:30:00.000+00:00"),
                               ("r2", "waiting", _at("20:00"))):
            c.execute("INSERT INTO runs(id, schedule, label, slot, night, state, error, "
                      "created_at, started_at) VALUES(?, 'news', 'World news', ?, "
                      "'2026-10-01', ?, 'The search failed', ?, ?)", (rid, at, state, at, at))
        c.execute("INSERT INTO guard_decisions(conversation_id, turn_id, message_id, "
                  "tool_call_id, tool, mode, layer, rule, does, decision, why, created_at) "
                  "VALUES(?, 't1', ?, 'b', 'browser__open', 'on', 2, 'hijack', 'stop', 'stop', "
                  "'sends their address out', ?)", (cid, m["id"], _at("10:02")))
        # Watch: a stop that would have been, its words Bastet's (banners.line)
        c.execute("INSERT INTO guard_decisions(conversation_id, turn_id, message_id, "
                  "tool_call_id, tool, mode, layer, rule, does, decision, class, sink, piece, "
                  "created_at) VALUES(?, 't1', ?, 'a', 'shell__run', 'watch', 1, 'leak', "
                  "'stop', 'would_stop', 'address', 'public', 'Elm St 4', ?)",
                  (cid, m["id"], _at("10:11")))
        c.execute("INSERT INTO freezes(at, by, why, conversation_id, turn_id, unfrozen_at, "
                  "unfrozen_by) VALUES(?, 'guard', 'it sent their address', ?, 't1', ?, 'user')",
                  (_at("10:12"), cid, _at("10:15")))
        c.execute("INSERT INTO guard_passes(rule, conversation_id, until, by, created_at) "
                  "VALUES('leak', ?, ?, 'user', ?)", (cid, _at("11:13"), _at("10:13")))
        c.execute("INSERT INTO work(kind, state, error, conversation_id, created_at) "
                  "VALUES('title', 'failed', 'no model answered', ?, ?)", (cid, _at("10:14")))
        return m["id"]

    return cid, await store.write(fn)


@pytest.mark.anyio
async def test_events_list_every_kind_newest_first_and_filter_and_page(store, monkeypatch):
    monkeypatch.setattr(events, "_now", lambda: datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
    cid, mid = await _rows(store)
    app = FastAPI()
    app.state.services = Services(cfg=CFG)
    app.state.services.store = store
    install_error_handler(app)
    app.include_router(events.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as h:
        async def get(**q):
            r = await h.get("/api/events", params=q)
            assert r.status_code == 200, r.text
            return r.json()

        got = await get()        # its tab: its chat's turns and calls, its messages
        assert [(e["kind"], e["title"], e["outcome"]) for e in got["events"]] == [
            ("turn", "Turn", "Failed"),
            ("message", "To Joe", "Sent"),
            ("message", "To Joe", "Sent"),
            ("tool", "Image generation: make images", "Running"),
            ("notebook", "Wrote people/noor.md", "Done"),
            ("tool", "Browser: open a page", "Failed"),
            ("tool", "Terminals: run a command", "Done"),
            ("turn", "Turn", "Done"),
        ]
        assert got["total"] == 8 and got["failed"] == 2 and got["next"] is None
        assert got["tabs"] == [
            {"id": "guard", "label": "Guard", "total": 5, "failed": 1},
            {"id": "celine", "label": "Celine", "total": 8, "failed": 2},
            {"id": "background", "label": "Background tasks", "total": 3, "failed": 2}]
        guard = await get(tab="guard")
        assert [(e["kind"], e["title"], e["outcome"]) for e in guard["events"]] == [
            ("freeze", "Unfreeze", "Done"),
            ("pass", "Pass on sending private data", "Allowed"),
            ("freeze", "Freeze", "Frozen"),
            ("guard", "Terminals: run a command", "Would have stopped"),
            ("guard", "Browser: open a page", "Stopped"),
        ]
        back = await get(tab="background")
        assert [(e["kind"], e["title"]) for e in back["events"]] == [
            ("work", "Title"), ("job", "Image generation: make images"), ("run", "World news")]
        every = got["events"] + guard["events"] + back["events"]
        by = {e["id"]: e for e in every}
        assert by["turn:t1"]["detail"] == "chat-model, 12 s"
        assert by["turn:t2"]["detail"] == "the model server did not answer"
        assert by["tool:a"]["detail"] == "ls -la" and by["tool:a"]["where"] == "find a job"
        assert by["tool:a"]["links"] == {"conversation": cid, "message": mid, "turn": "t1",
                                         "open": f"/chat/{cid}"}
        assert by["notebook:n"]["links"]["open"] == "/notebook?note=people/noor.md"
        assert by["notebook:n"]["detail"] == "who Noor is"
        assert by["job:J"]["detail"] == "2 min" and by["job:J"]["links"]["message"] == mid
        msgs = [e for e in got["events"] if e["kind"] == "message"]
        assert [m["detail"] for m in msgs] == ["both of you", "I found three."]
        msg = msgs[1]
        assert msg["detail"] == "I found three." and msg["links"]["open"].startswith("/intercom/")
        assert by["run:r1"]["links"]["open"] == "/routines/2026-10-01/run/r1"
        assert by["run:r1"]["detail"] == "The search failed"
        assert by["guard:1"]["detail"] == "sends their address out"
        assert by["guard:2"]["detail"] == ("Would have stopped a command. It sends "
                                           "\"Elm St 4\" (address) to the public.")
        assert by["freeze:1"]["detail"] == "Bastet: it sent their address"
        assert by["unfreeze:1"]["detail"] == "3 min"
        assert by["pass:1"]["detail"] == "until 13:13"        # Berlin
        assert by["work:1"]["detail"] == "no model answered"
        assert by["turn:t1"]["day"] == "Today" and by["run:r1"]["day"] == "Today"
        assert [k["id"] for k in got["kinds"]] == list(events.TAB_KINDS["celine"])
        assert got["days"][:2] == [{"value": "2026-10-01", "label": "Today"},
                                   {"value": "2026-09-30", "label": "Yesterday"}]

        for tab, kind, n in (("celine", "tool", 3), ("guard", "freeze", 2),
                             ("background", "job", 1), ("background", "turn", 0)):
            assert len((await get(tab=tab, kind=kind))["events"]) == n, kind
        failed = await get(state="failed")
        assert [e["id"] for e in failed["events"]] == ["turn:t2", "tool:b"]
        # the day is the app's (Berlin): its run at 23:30 UTC on the 30th is the 1st there
        assert [e["id"] for e in (await get(tab="background", day="2026-09-30"))["events"]] == []
        assert "run:r1" in [e["id"] for e in
                            (await get(tab="background", day="2026-10-01"))["events"]]

        first = await get(limit=4)
        assert [e["id"] for e in first["events"]] == [e["id"] for e in got["events"][:4]]
        assert first["total"] == 8
        seen = list(first["events"])
        nxt = first["next"]
        while nxt:
            page = await get(limit=4, before=nxt)
            assert page["total"] is None and page["failed"] is None   # the first page's
            seen += page["events"]
            nxt = page["next"]
        assert [e["id"] for e in seen] == [e["id"] for e in got["events"]]

        assert (await h.get("/api/events", params={"kind": "nope"})).status_code == 400
        assert (await h.get("/api/events", params={"kind": "run"})).status_code == 400
        assert (await h.get("/api/events", params={"tab": "nope"})).status_code == 400
        assert (await h.get("/api/events", params={"day": "soon"})).status_code == 400


@pytest.mark.anyio
async def test_bastets_filters_and_undecided_apart_from_her_stops(store, monkeypatch):
    monkeypatch.setattr(events, "_now", lambda: datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
    cid, mid = await _rows(store)
    await store.write(lambda c: c.execute(
        "INSERT INTO guard_decisions(conversation_id, turn_id, message_id, tool_call_id, tool, "
        "mode, layer, rule, does, decision, why, created_at) VALUES(?, 't1', ?, 'a', "
        "'shell__run', 'watch', 2, 'hijack', 'stop', 'unreachable', 'no answer', ?)",
        (cid, mid, _at("10:20"))))
    app = FastAPI()
    app.state.services = Services(cfg=CFG)
    app.state.services.store = store
    install_error_handler(app)
    app.include_router(events.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as h:
        async def get(**q):
            r = await h.get("/api/events", params=q)
            assert r.status_code == 200, r.text
            return r.json()

        got = await get(tab="guard")
        assert got["failed"] == 1 and got["undecided"] == 1   # not counted as failed
        by = {e["id"]: e for e in got["events"]}
        assert (by["guard:3"]["outcome"], by["guard:3"]["tone"]) == ("Could not decide", "warn")
        assert by["guard:1"]["guard"] == {"id": 1, "actions": ["once", "pass"]}
        assert by["guard:3"]["guard"] == {"id": 3, "actions": []}
        assert (await get())["events"][-1]["guard"] is None
        assert [e["id"] for e in (await get(tab="guard", state="undecided"))["events"]] == ["guard:3"]
        assert [e["id"] for e in (await get(tab="guard", rule="hijack"))["events"]] == ["guard:3", "guard:1"]
        watch = await get(tab="guard", mode="watch")
        assert [e["id"] for e in watch["events"]] == ["guard:3", "guard:2"]
        assert watch["total"] == 2 and watch["undecided"] == 1
        assert [e["id"] for e in (await get(tab="guard", decision="stop"))["events"]] == ["guard:1"]
        assert got["guard"]["modes"] == [{"id": "on", "label": "On"},
                                         {"id": "watch", "label": "Watch"}]
        assert {"id": "unreachable", "label": "Could not decide"} in got["guard"]["decisions"]
        assert {"id": "hijack", "label": "Following words from outside"} in got["guard"]["rules"]
        bad = await h.get("/api/events", params={"tab": "guard", "kind": "turn",
                                                     "mode": "on"})
        assert bad.status_code == 400
        assert (await h.get("/api/events", params={"state": "nope"})).status_code == 400


@pytest.mark.anyio
async def test_events_added_goes_on_the_feed_when_one_lands(store):
    """A finished tool call, a notebook write, a turn's end, a job's end, its message to
    the user and a run's end each put `events.added` on the global feed; a turn that only
    starts, or the user's own message, do not."""
    cid = await conversations.create(store, title="x")
    other = await conversations.create(store, title="y")

    def fn(c):
        store.event(c, cid, "turn.state", {"turn_id": "t", "state": "streaming"})
        store.event(c, cid, "tool.done", {"tool_call_id": "a", "name": "shell__run"})
        store.event(c, cid, "tool.done", {"tool_call_id": "b", "name": "notebook__edit"})
        store.event(c, cid, "job.state", {"job_id": "j", "state": "running"})
        store.event(c, cid, "job.state", {"job_id": "j", "state": "done"})
        store.event(c, cid, "turn.state", {"turn_id": "t", "state": "done"})
        insert(c, f"agent:{cid}", "user", "message", "hi", store=store)
        insert(c, f"agent:{cid}", f"agent:{other}, user", "message", "hi", store=store)
        insert(c, f"agent:{cid}", f"agent:{other}", "message", "hi", store=store)
        insert(c, "user", f"agent:{cid}", "message", "hi", store=store)
        c.execute("INSERT INTO runs(id, schedule, label, slot, night, state, created_at) "
                  "VALUES('r', 'news', 'World news', ?, '2026-10-01', 'running', ?)",
                  (_at("10:00"), _at("10:00")))
        from celine.schedules.runs import _set
        _set(c, store, CFG, "r", state="done")

    await store.write(fn)
    from celine import work
    await work.put(store, "title", {}, conversation_id=cid)
    rows = await store.read(lambda c: c.execute(
        "SELECT data FROM global_events WHERE type='events.added' ORDER BY id").fetchall())
    import json
    assert [json.loads(r[0]) for r in rows] == [
        {"kind": "tool", "conversation": cid}, {"kind": "notebook", "conversation": cid},
        {"kind": "job", "conversation": cid}, {"kind": "turn", "conversation": cid},
        {"kind": "message", "conversation": cid}, {"kind": "message", "conversation": cid},
        {"kind": "run", "conversation": None}, {"kind": "work", "conversation": cid}]


def test_a_queued_turn_reads_queued_not_running():
    r = {"state": "queued", "model": None, "backend_id": None, "at": _at("10:00"), "finished_at": None,
         "reason": None}
    got = events._turn(r, CFG)
    assert (got["outcome"], got["tone"]) == ("Queued", "muted")
