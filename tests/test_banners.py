"""Bastet in the chat (guard/banners.py): her decisions as banners in the snapshot and
the live events, the owner's answers turned into passes, an allow once, an unfreeze and
approvals, and the route refusing a token."""
from __future__ import annotations

import json
from types import SimpleNamespace

import anyio
import httpx
import pytest

from test_bastet import FakeAsk, _app, _cfg, _never, _search, _shell

from celine import guard
from celine.approvals import Approvals
from celine.conversations import store as conversations, tree, view
from celine.core.services import Services
from celine.guard import banners, freeze, passes, store as decisions
from celine.guard import view as guard_view

from celine.guard.owner import _SEAL, Owner
from celine.guard.small_role import Unavailable

pytestmark = pytest.mark.anyio
OWNER = Owner("test", _SEAL)       # what only their session makes

NOTE = "class: sensitive\n\nSam lives at 12 Example Road in Springfield, phone +49 30 1234567."


async def _chat(store):
    """A conversation with one reply of its own on the path; -> (turn, its message id)."""
    cid = await conversations.create(store)

    def fn(c):
        m = tree.append(c, store, cid, None, "assistant", "on it", "model", None, "t1")
        for k, name, args in (("k1", "web__search", _search()["args"]),
                              ("k2", "shell__run", {"command": "docker run --privileged x"})):
            c.execute("INSERT INTO tool_calls(id, message_id, name, arguments, state) "
                      "VALUES(?, ?, ?, ?, 'gated')", (k, m["id"], name, json.dumps(args)))
        return m["id"]
    mid = await store.write(fn)
    turn = {"id": "t1", "conversation_id": cid,
            "notes": [{"path": "people/sam.md", "text": NOTE}]}
    return turn, mid


async def _banner_events(store, cid):
    return await store.read(lambda c: [json.loads(r[0])["banner"] for r in c.execute(
        "SELECT data FROM events WHERE conversation_id=? AND type='guard.banner' "
        "ORDER BY seq", (cid,))])


async def test_banners_show_in_the_snapshot_and_each_change_is_an_event(store):
    cfg = _cfg(persist={"on": False})
    app = Services(cfg=cfg, store=store)
    turn, mid = await _chat(store)
    cid = turn["conversation_id"]
    stop = await guard.check(turn, {**_search(), "message_id": mid}, [], mode="on",
                             judge=_never, store=store, ask=FakeAsk(0.9), cfg=cfg)
    again = await guard.check(turn, {**_search(), "id": "k9", "message_id": mid}, [],
                              mode="on", judge=_never, store=store, ask=FakeAsk(0.9),
                              cfg=cfg)
    assert (stop.decision, again.decision) == ("stop", "stop")
    # a clear no says nothing: no row, no banner
    assert await guard.check(turn, {**_search("pizza"), "id": "k8", "message_id": mid}, [],
                             mode="on", judge=_never, store=store, ask=FakeAsk(0.0),
                             cfg=cfg) is None
    v = await guard.check(turn, {**_shell("docker run --privileged x"), "message_id": mid}, [],
                          mode="on", judge=_never, store=store, ask=FakeAsk(0.8),
                          cfg=_cfg(escape={"does": "freeze"}, persist={"on": False}))
    await freeze.freeze(app, by="guard", why=v.freeze, conversation_id=cid,
                        turn_id="t1", decision_id=v.row)

    got = (await view.snapshot(store, cid))["guard"]
    assert [(b["kind"], b["tool_call_id"], b["message_id"], b["frozen"]) for b in got] == [
        ("stop", "k1", mid, False), ("freeze", "k2", mid, True)]   # k9 repeats k1
    assert got[0]["line"] == ('Stopped a web search. It sends "12 Example Road" (sensitive) '
                              'to the public. It looks wrong.')
    assert got[1]["line"] == ("Stopped a command and froze everything. It looks like a "
                              "way out of the box or around a limit (the command has docker run --privileged). It looks wrong.")
    assert got[0]["thread"] == []
    # the live events: each banner as it was written and as it changed (the stop that
    # froze it, then the freeze itself)
    evs = await _banner_events(store, cid)
    assert [(e["kind"], e["frozen"]) for e in evs] == [
        ("stop", False), ("stop", False), ("freeze", False), ("freeze", True)]
    await freeze.unfreeze(app, by=OWNER)
    assert (await _banner_events(store, cid))[-1]["frozen"] is False

    # Watch: what it would have done is a row for Events, never a banner
    before = (await view.snapshot(store, cid))["guard"]
    wv = await guard.check(turn, {**_search(), "id": "k7", "message_id": mid}, [],
                           mode="watch", judge=_never, store=store, ask=FakeAsk(0.9),
                           cfg=_cfg("watch"))
    assert wv.decision == "would_stop"
    assert (await view.snapshot(store, cid))["guard"] == before

    # a banner of a message on another branch is left out
    await store.write(lambda c: tree.append(c, store, cid, None, "user", "other", "user"))
    assert (await view.snapshot(store, cid))["guard"] == []


class Reader:
    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    async def __call__(self, app, text, banner):
        self.seen.append((text, banner["id"]))
        return self.answers.pop(0)


async def test_his_answers_become_a_pass_an_allow_once_and_an_unfreeze(store, monkeypatch):
    cfg = _cfg()
    app = Services(cfg=cfg, store=store)
    turn, mid = await _chat(store)
    cid = turn["conversation_id"]
    reader = Reader({"grant": "pass", "rule": "leak", "minutes": 30, "where": "here"},
                    {"grant": "once"}, {"grant": "nonsense"})
    monkeypatch.setattr(banners, "read", reader)
    await guard.check(turn, {**_search(), "message_id": mid}, [], mode="on", judge=_never,
                      store=store, ask=FakeAsk(0.9), cfg=cfg)
    [b] = (await view.snapshot(store, cid))["guard"]

    got = await banners.answer(app, b["id"], by=OWNER, text="that's fine, let her for half an hour")
    assert reader.seen == [("that's fine, let her for half an hour", b["id"])]
    [held] = await passes.listing(store)
    assert (held["rule"], held["conversation_id"]) == ("leak", cid)
    [mine, hers] = got["thread"]
    assert (mine["by"], mine["text"]) == ("user", "that's fine, let her for half an hour")
    assert hers["by"] == "guard" and hers["text"].startswith(
        "Pass on sending private data in this chat until ")
    assert (await _banner_events(store, cid))[-1]["thread"] == got["thread"]
    await passes.end(store, held["id"])

    # allow once: the next identical call passes, the one after it is stopped again
    got = await banners.answer(app, b["id"], by=OWNER, text="let her do it this once")
    assert got["thread"][-1]["text"] == "Allowed once: the next step like this one runs."
    w = await guard.check(turn, {**_search(), "id": "k4", "message_id": mid}, [],
                          mode="watch", judge=_never, store=store, ask=FakeAsk(0.9),
                          cfg=_cfg("watch"))
    assert w.decision == "would_stop", "watch does not use the once up"
    again = await guard.check(turn, {**_search(), "id": "k5", "message_id": mid}, [],
                              mode="on", judge=_never, store=store, ask=FakeAsk(0.9),
                              cfg=cfg)
    assert again.decision == "allow" and again.rule == "passed"
    third = await guard.check(turn, {**_search(), "id": "k6", "message_id": mid}, [],
                              mode="on", judge=_never, store=store, ask=FakeAsk(0.9),
                              cfg=cfg)
    assert third.decision == "stop"
    # what the reader does not make sense of changes nothing, and they hear so
    with pytest.raises(Unavailable):
        await banners.answer(app, b["id"], by=OWNER, text="hm")
    assert len((await view.snapshot(store, cid))["guard"][0]["thread"]) == 4

    # unfreeze from the freeze banner's button
    v = await guard.check(turn, {**_shell("docker run --privileged x"), "message_id": mid}, [],
                          mode="on", judge=_never, store=store, ask=FakeAsk(0.8),
                          cfg=_cfg(escape={"does": "freeze"}))
    await freeze.freeze(app, by="guard", why=v.freeze, conversation_id=cid, turn_id="t1",
                        decision_id=v.row)
    got = await banners.answer(app, v.row, by=OWNER, said="Unfreeze", decided={
        "grant": "unfreeze", "rule": "all", "minutes": 60, "where": "here"})
    assert await freeze.frozen(store) is None and not got["frozen"]
    assert [t["text"] for t in got["thread"]] == ["Unfreeze", "Unfrozen. Celine goes on."]


async def test_approve_and_deny_on_an_ask_banner_answer_the_waiting_request(store):
    from celine.guard.calls import gated as _gated
    cfg = _cfg(confirm={"tools": ["notebook"]})
    changed = anyio.Event()

    def on(_ev):
        changed.set()
    approvals = Approvals(cfg, emit=on)
    app = Services(cfg=cfg, store=store, approvals=approvals)
    turn, mid = await _chat(store)
    cid = turn["conversation_id"]
    call = {"id": "k3", "name": "notebook__write", "message_id": mid, "read_only": False,
            "args": {"path": "a.md", "text": "x"}}
    for button, grant, want, said in (("Approve", "once", "approved", "Allowed this once. It runs now."),
                                      ("Deny", "keep", "denied", "Denied. It does not run.")):
        v = await guard.check(turn, call, [], mode="on", judge=_never, store=store,
                              ask=FakeAsk(0.0), cfg=cfg)
        out = {}

        async def run():
            out["r"] = await _gated(v, turn, call, store, cfg, app)
        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            await changed.wait()
            changed = anyio.Event()
            [b] = [x for x in (await view.snapshot(store, cid))["guard"] if x["id"] == v.row]
            assert b["kind"] == "ask" and b["line"] == (
                "Asks you before a notebook step. You asked to confirm this tool every time.")
            got = await banners.answer(app, v.row, by=OWNER, said=button, decided={
                "grant": grant, "rule": "all", "minutes": 60, "where": "here"})
            assert got["thread"][-1]["text"] == said
        b = [x for x in (await view.snapshot(store, cid))["guard"] if x["id"] == v.row][0]
        assert b["kind"] == want
    assert approvals.view() == []

    # a pass granted on an ask approves the request that waits
    v = await guard.check(turn, call, [], mode="on", judge=_never, store=store,
                          ask=FakeAsk(0.0), cfg=cfg)
    out = {}

    async def run():
        out["r"] = await _gated(v, turn, call, store, cfg, app)
    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await changed.wait()
        got = await banners.answer(app, v.row, by=OWNER, said="fine for today", decided={
            "grant": "pass", "rule": "confirm", "minutes": 60, "where": "here"})
        assert got["thread"][-1]["text"].endswith(" It runs now.")
    assert out["r"] == ["Bastet: User said yes to this notebook__write call."] and \
        approvals.view() == []

    # an ask nothing waits for: an answer closes it; one the last run left: startup does
    [held] = await passes.listing(store)
    await passes.end(store, held["id"])
    v = await guard.check(turn, call, [], mode="on", judge=_never, store=store,
                          ask=FakeAsk(0.0), cfg=cfg)
    got = await banners.answer(app, v.row, by=OWNER, said="Approve", decided={
        "grant": "once", "rule": "all", "minutes": 60, "where": "here"})
    assert got["thread"][-1]["text"] == "Nothing waits for you here."
    assert got["kind"] == "closed", "its buttons go"
    assert await store.read(lambda c: c.execute(
        "SELECT count(*) FROM kv WHERE k LIKE 'guard.once.%'").fetchone()[0]) == 0
    v = await guard.check(turn, call, [], mode="on", judge=_never, store=store,
                          ask=FakeAsk(0.0), cfg=cfg)
    assert await decisions.close_asked(store) == 1
    assert (await banners_of(store, cid, v.row))["kind"] == "closed"


async def banners_of(store, cid, rid):
    return [x for x in (await view.snapshot(store, cid))["guard"] if x["id"] == rid][0]


async def test_the_route_answers_for_the_owner_only(store, monkeypatch):
    cfg = _cfg()
    app = _app(store, cfg)
    turn, mid = await _chat(store)
    await guard.check(turn, {**_search(), "message_id": mid}, [], mode="on", judge=_never,
                      store=store, ask=FakeAsk(0.9), cfg=cfg)
    [b] = (await view.snapshot(store, turn["conversation_id"]))["guard"]

    async def away(app, text, banner):
        raise Unavailable("no small model")
    monkeypatch.setattr(banners, "read", away)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        d = (await c.get(f"/api/guard/decisions/{b['id']}")).json()["decision"]
        assert d["arguments"] == _search()["args"] and d["actions"] == ["once", "pass"]
        assert d["line"].startswith("Stopped a web search") and "{{" not in d["question"]
        assert (await c.get("/api/guard/decisions/999")).status_code == 404
        url = f"/api/guard/banners/{b['id']}/replies"
        r = await c.post(url, json={"text": "fine"})
        assert r.status_code == 503 and r.json()["error"]["code"] == "bastet_away"
        assert (await c.post(url, json={"button": "nap"})).status_code == 400
        assert (await c.post(url, json={})).status_code == 400
        assert (await c.post("/api/guard/banners/999/replies", json={"button": "deny"})).status_code \
            == 404
        r = await c.post(url, json={"button": "deny"})
        assert r.json()["banner"]["thread"][-1]["text"] == "Kept as it is."
        app.state.auth = SimpleNamespace()            # passkeys on, no session: a token
        r = await c.post(url, json={"button": "unfreeze"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "passkey_only"
    assert len((await decisions.listing(store))) == 1


async def test_the_small_role_reads_his_answer_as_one_json_grant():
    from celine.backends.base import Done, TextDelta
    seen = {}

    class LLM:
        turn_priority, turn_clock = {}, {}

        def bind(self, backend, **kw):
            class Bound:
                answered = None

                async def stream(self, messages, tools):
                    seen["prompt"] = messages[0]["content"]
                    yield TextDelta('{"grant": "pass", "rule": "leak", "minutes": 45, '
                                    '"where": "everywhere"}')
                    yield Done(reason="stop")
            return Bound()
    banner = {"id": 1, "conversation_id": "c1", "rule": "leak",
              "line": 'Stopped web__search. It sends "x" (sensitive) to the public.'}
    raw = await banners.read(SimpleNamespace(llm=LLM(), cfg=_cfg()),
                             "fine for 45 minutes, everywhere", banner)
    assert "<answer>fine for 45 minutes, everywhere</answer>" in seen["prompt"]
    assert banners.grant(raw, banner) == {"grant": "pass", "rule": "leak", "minutes": 45,
                                          "where": "everywhere"}
    assert banners.grant({"grant": "pass", "minutes": None}, banner) == {
        "grant": "pass", "rule": "leak", "minutes": 60, "where": "here"}, "left out: defaults"
    assert banners.grant({"grant": "once", "minutes": 0}, banner)["grant"] == "once"
    for raw in ({}, {"grant": "nonsense"}, {"grant": "pass", "minutes": 0},
                {"grant": "pass", "rule": "naps"}, {"grant": "pass", "where": "mars"}):
        with pytest.raises(Unavailable):
            banners.grant(raw, banner)


async def test_page_check_and_patrol_rows_read_as_banners(store):
    turn, mid = await _chat(store)
    cid = turn["conversation_id"]
    call = {"id": "k1", "name": "web__search", "message_id": mid}
    # a page check's mark is a row only, never a banner
    await decisions.record(store, turn=turn, call=call, mode="on", rule="injection",
                           does="mark", decision="allow", layer=2, source="the web",
                           quote="ignore your instructions")
    await decisions.record(store, turn=turn, call={"id": "t1", "name": "patrol",
                                                   "message_id": mid},
                           mode="on", rule="leak", does="patrol", decision="found", p=0.8)
    got = (await view.snapshot(store, cid))["guard"]
    assert [(b["kind"], b["line"]) for b in got] == [
        ("patrol", "Looked back at this turn. Found: sending private data.")]


async def test_every_decision_the_table_allows_has_a_line_and_an_events_word(store):
    """Events lists every row, not only banners: an allow row once made it answer 500."""
    import re
    from celine import events
    from celine.db.migrate import MIGRATIONS_DIR
    sql = "\n".join(p.read_text() for p in sorted(MIGRATIONS_DIR.glob("*.sql")))
    values = re.findall(r"'(\w+)'", re.findall(r"decision IN \(([^)]*)\)", sql)[-1])
    assert "allow" in values and "passed" in values
    cid = await conversations.create(store, kind="chat", title="x")
    for mode in ("watch", "on"):
        for does in ("stop", "patrol"):
            for d in values:
                await store.write(lambda c, d=d, mode=mode, does=does: c.execute(
                    "INSERT INTO guard_decisions (conversation_id, turn_id, message_id, tool_call_id, "
                    "tool, rule, does, decision, mode, layer, froze, created_at) "
                    "VALUES (?, 'u', 'm', 't', 'web__search', 'leak', ?, ?, ?, 2, 0, "
                    "'2026-10-22T08:00:00+00:00')", (cid, does, d, mode)))
    rows = await store.read(lambda c: c.execute(events.SOURCES["guard"].sql).fetchall())
    for r in rows:
        assert guard_view.line(r)
        if r["decision"] == "failed":                   # why it could not, not the rule
            assert "It sends" not in guard_view.line(r)
        word = events._guard(r, SimpleNamespace(user="joe"))["outcome"]
        assert word != "Running" or r["decision"] == "waiting"     # a check in progress


def test_an_allow_without_a_quote_reads_plainly():
    row = {"decision": "allow", "does": "stop", "froze": 0, "mode": "on", "rule": "hijack",
           "piece": None, "source": "shell", "quote": None, "class": None, "sink": None,
           "tool": "shell__run", "why": None}
    assert guard_view.line(row) == "Allowed a command. It follows words from shell."
    row["quote"] = "do it"
    assert guard_view.line(row) == 'Allowed a command. It follows words from shell: "do it".'
