"""Bastet's escalation chain (server/celine/guard/chain.py): the second look (L2) on an
L1 yes, against a fake model server: its verdict and why on the row, a no that lifts the
freeze L1 made and keeps the owner's note back, a patrol finding it says no to never
reported, and L1 standing when L2 is away or not configured."""
from __future__ import annotations

import json

import httpx
import pytest

from test_banners import _chat
from test_bastet import FakeAsk, _cfg, _never, _shell
from test_bastet_patrol import AT, _app, _finished, _notes

from celine import guard
from celine.core.services import Services
from celine.guard import chain, freeze, patrol
from celine.guard import store as decisions
from celine.guard.small_role import Unavailable

pytestmark = pytest.mark.anyio


def _l2cfg(mode="on", **saved):
    """A saved chain on the provider gpu-server (the shipped default names none)."""
    chain_ = {"provider": "gpu-server", "model": "judge-model", **saved.pop("chain", {})}
    cfg = _cfg(mode, chain=chain_, **saved)
    cfg.settings.providers = {"gpu-server": {"kind": "llama-swap", "base_url": "http://l2:11434"}}
    return cfg


def _server(verdict: str, quote: str, why: str, seen: list):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-priority"),
                     json.loads(request.content)))
        said = json.dumps({"verdict": verdict, "quote": quote, "why": why})
        return httpx.Response(200, json={"choices": [{"message": {"content": said}}]})
    return httpx.MockTransport(handle)


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
    seen: list = []
    app = Services(cfg=cfg, store=store)
    said = await chain.after_stop(app, v.row, v.note, "c1", "t1", transport=_server(
        "Y", "docker run --privileged", "It runs a privileged container.", seen))
    assert said == "yes"
    [(url, prio, body)] = seen
    assert url.endswith("/v1/chat/completions") and prio == "background"
    assert body["model"] == "judge-model" and body["temperature"] <= 0.3
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
    app = Services(cfg=cfg, store=store)
    await freeze.freeze(app, by="guard", why=v.freeze, conversation_id="c1",
                        turn_id="t1", decision_id=v.row)
    before = len(await _notes(store))
    said = await chain.after_stop(app, v.row, v.note, "c1", "t1", transport=_server(
        "N", "", "She lists her own containers.", []))
    assert said == "no" and await freeze.frozen(store) is None
    notes = await _notes(store)
    assert len(notes) == before + 1 and notes[-1].startswith(
        "I lifted the freeze on Celine myself") and "She lists her own containers." in notes[-1]
    row = await store.read(lambda c: dict(c.execute(
        "SELECT unfrozen_by FROM freezes").fetchone()))
    assert row["unfrozen_by"] == "bastet"


async def test_l2_waits_at_most_its_seconds_and_a_timeout_is_unreachable(store):
    cfg = _l2cfg(chain={"seconds": 42})
    v = await _stop(store, cfg)
    seen = []

    def slow(request):
        seen.append(request.extensions["timeout"])
        raise httpx.ReadTimeout("swapping", request=request)
    got = await chain.review(Services(cfg=cfg, store=store), v.row,
                             transport=httpx.MockTransport(slow))
    assert got == "unreachable" and seen[0]["read"] == 42.0


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

    def down(request):
        raise httpx.ConnectError("down")
    app = Services(cfg=cfg, store=store)
    assert await chain.after_stop(app, v.row, v.note, "c1", "t1",
                                  transport=httpx.MockTransport(down)) == "unreachable"
    [note] = await _notes(store)
    assert note.startswith("Bastet stopped a command"), "L1's stop stands: the owner hears it"
    assert note.endswith("The second look did not answer, so this stands: "
                         "judge-model at http://l2:11434/v1/chat/completions is not "
                         "reachable (ConnectError)."), "and why the second look did not change it"
    off = await chain.review(Services(cfg=_cfg(), store=store), v.row)
    assert off == "off"
    one = await decisions.one(store, v.row)
    assert [x["verdict"] for x in decisions.levels(one)] == ["Y", "unreachable", "off"]


async def test_a_patrol_finding_l2_says_no_to_is_never_reported(store, monkeypatch):
    await _finished(store, 1, command="rm -rf ~/projects")

    async def no(l2, text, transport=None):
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

    async def away(l2, text, transport=None):
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
    seen: list = []
    await chain.review(Services(cfg=cfg, store=store), v.row, transport=_server(
        "N", "", "Nothing here.", seen))
    body = seen[0][2]
    assert "response_format" not in body, "a backend's JSON mode mangles the keys"
    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == chain.TOKENS
    cfg = _l2cfg(chain={"thinking": "low"})
    seen.clear()
    await chain.review(Services(cfg=cfg, store=store), v.row, transport=_server(
        "N", "", "Nothing here.", seen))
    body = seen[0][2]
    assert body["reasoning_effort"] == "low"
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["max_tokens"] == chain.THINKING_TOKENS


def _replies(message: dict):
    return httpx.MockTransport(lambda request: httpx.Response(
        200, json={"choices": [{"message": message}]}))


async def test_an_answer_in_reasoning_content_counts(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    said = json.dumps({"verdict": "N", "quote": "", "why": "Her own box."})
    got = await chain.review(Services(cfg=cfg, store=store), v.row, transport=_replies(
        {"content": "", "reasoning_content": said}))
    assert got == "no"
    assert decisions.levels(await decisions.one(store, v.row))[-1]["why"] == "Her own box."


async def test_no_json_anywhere_is_no_answer_and_l1_stands(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    app = Services(cfg=cfg, store=store)
    for message in ({"content": ""}, {"content": None, "reasoning_content": "hmm, so"},
                    # what a backend's JSON mode made of the answers
                    {"content": '{"/verdict/": "Y", "/quote/": "docker run --privileged"}'},
                    {"content": '{"/!\\\\_ERROR\\\\_!/": "Invalid JSON format"}'}):
        assert await chain.after_stop(app, v.row, v.note, "c1", "t1",
                                      transport=_replies(message)) == "unreachable"
    assert len(await _notes(store)) == 4, "L1's stop stands: the owner hears it"
    last = decisions.levels(await decisions.one(store, v.row))[-1]
    assert last["verdict"] == "unreachable" and \
        last["why"] == "judge-model gave no verdict"


async def test_a_made_up_quote_is_a_no_and_quote_marks_around_a_real_one_are_a_yes(store):
    cfg = _l2cfg()
    v = await _stop(store, cfg)
    app = Services(cfg=cfg, store=store)
    assert await chain.review(app, v.row, transport=_server("Y", "rm -rf /", "made up", [])) \
        == "no"
    assert await chain.review(app, v.row, transport=_server(
        "Y", '"docker run --privileged"', "A privileged container.", [])) == "yes"


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
    asked: list = []
    up = True

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append((str(request.url), json.loads(request.content)["model"]))
        if not up:
            raise httpx.ConnectError("refused", request=request)
        said = json.dumps({"verdict": "Y", "quote": "docker run --privileged", "why": "Root."})
        return httpx.Response(200, json={"choices": [{"message": {"content": said}}]})
    real = chain.ask
    monkeypatch.setattr(chain, "ask", lambda l2, text, transport=None: real(
        l2, text, httpx.MockTransport(handle)))
    v = await _stop(store, cfg)
    turn = {"id": "t1", "conversation_id": "c1"}
    for up in (True, False):
        async with anyio.create_task_group() as tg:
            app = Services(cfg=cfg, store=store)
            app.dispatcher = type("D", (), {"jobs": type("J", (), {"tg": tg})})()
            await calls.gated(v, turn, {"name": "shell__run"}, store, cfg, app)
    assert asked == [("http://192.168.1.9:11434/v1/chat/completions", "small")] * 2
    yes, down = await _notes(store)
    assert yes.startswith("Bastet stopped a command") and "second look" not in yes
    assert down.endswith("The second look did not answer, so this stands: small at "
                         "http://192.168.1.9:11434/v1/chat/completions is not reachable "
                         "(ConnectError).")
