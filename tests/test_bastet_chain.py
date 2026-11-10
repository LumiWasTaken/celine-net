"""Bastet's escalation chain (server/celine/guard/chain.py): the second look (L2) on an
L1 yes, through the app's model layer with a fake model: its verdict and why on the row,
a no that lifts the freeze L1 made and keeps the owner's note back, a patrol finding it
says no to never reported, and L1 standing when L2 is away or not configured."""
from __future__ import annotations

import json

import anyio
import pytest

from test_banners import _chat
from test_bastet import FakeAsk, _cfg, _never, _shell
from test_bastet_patrol import AT, _app, _finished, _notes

from celine import guard
from celine.backends.base import BackendError, Done, ReasoningDelta, TextDelta
from celine.backends.llm import LLM
from celine.backends.openai import LlamaServerBackend
from celine.backends.queue import Priority
from celine.core.services import Services
from celine.guard import chain, freeze, patrol
from celine.guard import store as decisions
from celine.guard.small_role import Unavailable

pytestmark = pytest.mark.anyio


class L2(LlamaServerBackend):
    """The second look's model: keeps each request's model and body, answers `said`
    ({content, reasoning}), or raises it when it is an error."""
    said: dict | Exception = {}
    calls: list[tuple[str, dict]] = []

    async def stream(self, messages, tools, body=None, model=None):
        L2.calls.append((self.model, self.request_body(messages, tools, body, model=model)))
        if isinstance(L2.said, Exception):
            raise L2.said
        if L2.said == {"never": True}:
            await anyio.Event().wait()           # never set: it never answers
        if L2.said.get("reasoning"):
            yield ReasoningDelta(L2.said["reasoning"])
        yield TextDelta(L2.said.get("content") or "")
        yield Done(reason="stop")


def _says(verdict: str, quote: str, why: str) -> dict:
    return {"content": json.dumps({"verdict": verdict, "quote": quote, "why": why})}


def _l2cfg(mode="on", **saved):
    """A saved chain on the provider gpu-server (the shipped default names none)."""
    chain_ = {"provider": "gpu-server", "model": "judge-model", **saved.pop("chain", {})}
    cfg = _cfg(mode, chain=chain_, **saved)
    cfg.settings.providers = {"gpu-server": {
        "kind": "llama-swap", "base_url": "http://l2:11434",
        "models": {"judge-model": {"caps": ["chat"]}}}}
    return cfg


def _with(cfg, store, said) -> Services:
    """The app with its model layer on the fake L2 answering `said`."""
    L2.said, L2.calls = said, []
    return Services(cfg=cfg, store=store, llm=LLM(cfg, build=lambda bc: L2(bc, 5)))


async def _stop(store, cfg):
    """L1 stops its call k2 (a tool_calls row: docker run --privileged x)."""
    turn, mid = await _chat(store)
    v = await guard.check(turn, {**_shell("docker run --privileged x"), "message_id": mid},
                          [], mode="on", judge=_never, store=store, ask=FakeAsk(0.9),
                          cfg=cfg)
    assert v.decision == "stop"
    return v


async def test_a_yes_confirms_and_the_owner_hears_it(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    app = _with(cfg, store, _says("Y", "docker run --privileged",
                                  "It runs a privileged container."))
    sched, seen = app.llm.scheduler, []
    acquire = sched.acquire

    async def recorded(t, **kw):
        seen.append(t.priority)
        return await acquire(t, **kw)
    sched.acquire = recorded
    said = await chain.after_stop(app, v.row, v.note, "c1", "t1")
    assert said == "yes" and seen == [Priority.BACKGROUND], "in the queue, behind the turns"
    [(model, body)] = L2.calls
    assert model == "judge-model" and body["temperature"] <= 0.3
    assert "docker run --privileged" in body["messages"][0]["content"]
    one = await decisions.one(store, v.row)
    levels = decisions.levels(one)
    assert [(x["level"], x["verdict"]) for x in levels] == [("L1", "Y"), ("L2", "Y")]
    assert levels[1]["why"] == "It runs a privileged container."
    [note] = await _notes(store)
    assert note.startswith("Bastet stopped a command")


async def test_a_no_lifts_the_freeze_l1_made_and_keeps_the_note_back(store):
    cfg = _l2cfg(escape={"does": "freeze"})
    v = await _stop(store, cfg)
    app = _with(cfg, store, _says("N", "", "She lists her own containers."))
    await freeze.freeze(app, by="guard", why=v.freeze, conversation_id="c1",
                        turn_id="t1", decision_id=v.row)
    before = len(await _notes(store))
    said = await chain.after_stop(app, v.row, v.note, "c1", "t1")
    assert said == "no" and await freeze.frozen(store) is None
    notes = await _notes(store)
    assert len(notes) == before + 1 and notes[-1].startswith(
        "I unfroze Celine myself") and "She lists her own containers." in notes[-1]
    row = await store.read(lambda c: dict(c.execute(
        "SELECT unfrozen_by FROM freezes").fetchone()))
    assert row["unfrozen_by"] == "bastet"


async def test_l2_waits_at_most_its_seconds_and_a_timeout_is_unreachable(store):
    cfg = _l2cfg()
    app = _with(cfg, store, {"never": True})
    l2 = {**chain.level2(cfg), "seconds": 0}
    with pytest.raises(Unavailable, match="^judge-model took longer than 0 s$"):
        await chain.ask(app.llm, l2, "x")


async def test_a_patrol_finding_l2_said_no_to_does_not_count_as_told(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    cid = await store.read(lambda c: c.execute(
        "SELECT conversation_id FROM guard_decisions WHERE id=?", (v.row,)).fetchone()[0])
    turn = {"id": "t9", "conversation_id": cid}
    call = {"id": "t9", "name": "patrol", "message_id": None}
    for decision in ("allow", "found"):
        await decisions.record(store, turn=turn, call=call, mode="on", rule="damage",
                               does="patrol", decision=decision, piece="same", layer=2)
    rows = await decisions.listing(store, rule="damage")
    found, rejected = rows[0]["id"], rows[1]["id"]
    assert not await decisions.told(store, cid, found, 24), "the rejected one is no telling"
    assert await decisions.told(store, cid, rejected, 24)


async def test_l1_stands_when_l2_is_away_or_not_configured(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    app = _with(cfg, store, BackendError("gpu-server is not reachable (ConnectError)."))
    assert await chain.after_stop(app, v.row, v.note, "c1", "t1") == "unreachable"
    [note] = await _notes(store)
    assert note.startswith("Bastet stopped a command"), "L1's stop stands: the owner hears it"
    assert note.endswith("The Second look model did not answer, so the first decision stands. "
                         "gpu-server is not reachable (ConnectError)."), \
        "and why the second look did not change it"
    off = await chain.review(Services(cfg=_cfg(), store=store), v.row)
    assert off == "off"
    one = await decisions.one(store, v.row)
    assert [x["verdict"] for x in decisions.levels(one)] == ["Y", "unreachable", "off"]


async def test_a_patrol_finding_l2_says_no_to_is_never_reported(store, monkeypatch):
    await _finished(store, 1, command="rm -rf ~/projects")

    async def no(llm, l2, text):
        assert "rm -rf ~/projects" in text
        return {"verdict": "N", "quote": "", "why": "Her own scratch folder.",
                "model": "judge-model", "ms": 5}
    monkeypatch.setattr(chain, "ask", no)
    app = Services(cfg=_l2cfg(), store=store)
    got = await patrol.once(app, at=AT, judge=FakeAsk(0.9))
    assert got["findings"] == [] and await _notes(store) == []
    [row] = await decisions.listing(store)
    assert row["decision"] == "allow" and row["scores"]["chain"][-1]["why"] == \
        "Her own scratch folder."

    async def away(llm, l2, text):
        raise Unavailable("swapping")
    monkeypatch.setattr(chain, "ask", away)
    await _finished(store, 1, command="rm -rf ~/projects", tag="b",
                    at="2026-10-22T03:30:00.000+00:00")
    got = await patrol.once(app, at="2026-10-22T04:00:00.000+00:00", judge=FakeAsk(0.9))
    assert len(got["findings"]) == 1, "L2 away: L1's finding stands"
    assert _app  # the patrol tests' helper stays importable


async def test_l2_thinks_as_chain_thinking_says_off_by_default(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    await chain.review(_with(cfg, store, _says("N", "", "Nothing here.")), v.row)
    [(_, off)] = L2.calls
    assert "response_format" not in off, "a backend's JSON mode mangles the keys"
    assert off["max_tokens"] == chain.TOKENS
    cfg = _l2cfg(chain={"thinking": "low"})
    await chain.review(_with(cfg, store, _says("N", "", "Nothing here.")), v.row)
    [(_, low)] = L2.calls
    assert low["max_tokens"] == chain.THINKING_TOKENS
    assert off != {**low, "max_tokens": off["max_tokens"]}, "the level reaches the request"


async def test_an_answer_in_its_reasoning_counts(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    said = json.dumps({"verdict": "N", "quote": "", "why": "Her own box."})
    got = await chain.review(_with(cfg, store, {"content": "", "reasoning": said}), v.row)
    assert got == "no"
    assert decisions.levels(await decisions.one(store, v.row))[-1]["why"] == "Her own box."


async def test_no_json_anywhere_is_no_answer_and_l1_stands(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    for said in ({"content": ""}, {"content": "", "reasoning": "hmm, so"},
                 # what a backend's JSON mode made of the answers
                 {"content": '{"/verdict/": "Y", "/quote/": "docker run --privileged"}'},
                 {"content": '{"/!\\\\_ERROR\\\\_!/": "Invalid JSON format"}'}):
        assert await chain.after_stop(_with(cfg, store, said), v.row, v.note, "c1",
                                      "t1") == "unreachable"
    assert len(await _notes(store)) == 4, "L1's stop stands: the owner hears it"
    last = decisions.levels(await decisions.one(store, v.row))[-1]
    assert last["verdict"] == "unreachable" and \
        last["why"] == "judge-model gave no decision"


async def test_a_made_up_quote_is_a_no_and_quote_marks_around_a_real_one_are_a_yes(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    assert await chain.review(_with(cfg, store, _says("Y", "rm -rf /", "made up")),
                              v.row) == "no"
    assert await chain.review(_with(cfg, store, _says(
        "Y", '"docker run --privileged"', "A privileged container.")), v.row) == "yes"


async def test_the_second_look_saved_on_the_guard_page_is_asked_and_its_failure_is_told(
        store, tmp_path, monkeypatch):
    """End to end: PUT /guard/rules as the Guard page saves it, an L1 stop in its turn
    (calls.gated), the background second look on exactly that provider and model; down,
    the owner's note says the stop stands and why."""
    import anyio
    from settings_fixtures import client, make_app, scratch

    from celine import boot
    from celine.guard import calls
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    async with client(make_app(cfg, store)) as c:
        r = await c.put("/api/guard/rules", json={"chain": {"provider": "lan", "model": "small"}})
        assert r.status_code == 200
    v = await _stop(store, cfg)
    turn = {"id": "t1", "conversation_id": "c1"}
    asked = []
    for said in (_says("Y", "docker run --privileged", "Root."),
                 BackendError("LAN is not reachable (ConnectError).")):
        async with anyio.create_task_group() as tg:
            app = _with(cfg, store, said)
            app.dispatcher = type("D", (), {"jobs": type("J", (), {"tg": tg})})()
            await calls.gated(v, turn, {"name": "shell__run"}, store, cfg, app)
        asked += [m for m, _ in L2.calls]
    assert asked == ["small", "small"]
    yes, down = await _notes(store)
    assert yes.startswith("Bastet stopped a command") and "second look" not in yes
    assert down.endswith("The Second look model did not answer, so the first decision stands. LAN is not "
                         "reachable (ConnectError).")


async def test_an_error_answer_of_l2_is_unavailable_with_its_words(store):
    cfg = _l2cfg()
    app = _with(cfg, store, BackendError("gpu-server returned 503: loading"))
    with pytest.raises(Unavailable, match="^gpu-server returned 503: loading$"):
        await chain.ask(app.llm, chain.level2(cfg), "x")
