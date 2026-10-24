"""The user's approvals (celine/approvals.py): an approve or a deny reaches the waiting tool,
silence times out, the limits hold, a denial is just a denial, a lock denies what
waits, no reason means no request, and the request names the last outside read."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from chat_fixtures import until

from celine import approvals as ap

from celine.guard.owner import _SEAL, Owner

pytestmark = pytest.mark.anyio
OWNER = Owner("test", _SEAL)       # what only their session makes


def _taps(monkeypatch, ask_for=5):
    monkeypatch.setattr(ap, "ASK_FOR", ask_for)
    return ap.Approvals(SimpleNamespace(user="sam", name="Celine"))


WHY = "the order script needs to log in"


async def _waits(a):
    await until(lambda: a.waiting)


async def _answer(a, ok):
    await _waits(a)
    [aid] = a.waiting
    assert a.decide(aid, ok, OWNER)


async def test_approve_and_deny_reach_the_tool_and_the_view_names_the_outside_read(monkeypatch):
    a = _taps(monkeypatch)
    a.seen("c1", "browser__open")
    a.seen("c1", "notebook__read")              # not from outside: the browser stays named
    action = ap.SHELL.format(entry="example-shop")
    task = asyncio.create_task(a.ask(action, ["The file: x"], "c1", WHY,
                                     ap.SHELL_SHORT.format(entry="example-shop")))
    await _waits(a)
    [view] = a.view()
    assert view["read"] == "browser__open" and view["why"] == WHY
    await _answer(a, True)
    assert await task is None and a.waiting == {}
    got, _ = await asyncio.gather(a.ask(action, [], "c2", WHY), _answer(a, False))
    assert got == "sam denied it; nothing was done."
    assert not a.decide("nope", True, OWNER)


async def test_silence_times_out_and_the_view_has_the_fixed_fields(monkeypatch):
    a = _taps(monkeypatch)
    task = asyncio.create_task(a.ask(ap.ROTATE.format(entry="x"), ["Its sites: none"], "c1", WHY))
    await _waits(a)
    [view] = a.view()
    assert set(view) == {"id", "kind", "action", "facts", "why", "conversation", "read",
                         "expires_in"} and view["kind"] == "vault"
    assert view["why"] == WHY
    assert view["action"] == "change the password of x" and view["read"] is None
    a.forget()
    await task
    a = _taps(monkeypatch, ask_for=0)
    got = await a.ask(ap.ROTATE.format(entry="x"), [], "c1", WHY)
    assert got == "sam did not answer in 1 minute; nothing was done."


async def test_one_at_a_time_no_hourly_cap_and_denials_lock_nothing(monkeypatch):
    a = _taps(monkeypatch)
    first = asyncio.create_task(a.ask("a", [], "c", WHY))
    await _waits(a)
    assert (await a.ask("b", [], "c", WHY)).startswith("Another request waits for sam")
    await _answer(a, True)
    assert await first is None
    for _ in range(8):                           # as many as it needs, one at a time
        got, _ = await asyncio.gather(a.ask("a", [], "c", WHY), _answer(a, True))
        assert got is None

    a = _taps(monkeypatch)
    for _ in range(2):
        await asyncio.gather(a.ask("a", [], "c", WHY), _answer(a, False))
    got, _ = await asyncio.gather(a.ask("a", [], "c", WHY), _answer(a, True))
    assert got is None                           # two denials, the next one still asks


async def test_a_lock_denies_what_waits_and_no_reason_asks_nothing(monkeypatch):
    a = _taps(monkeypatch)
    waiting = asyncio.create_task(a.ask("a", [], "c", WHY))
    await _waits(a)
    a.forget()                                   # the vault locked
    assert await waiting == "sam denied it; nothing was done."
    for why in ("", "   ", "need it"):
        got = await a.ask("a", [], "c", why)
        assert got.startswith("Nothing was asked: say why") and not a.waiting


def test_an_entrys_sites_are_exact_https_origins():
    from celine.vault import origin, origins
    assert origin("example-shop.com") == "https://example-shop.com:443"
    assert origin("https://Example-Shop.com./login?x=1") == "https://example-shop.com:443"
    assert origin("https://example-shop.com:8443") == "https://example-shop.com:8443"
    for bad in ("http://example-shop.com", "https://example-shop.com@evil.example", "https://u:p@a.com",
                "ftp://a.com", "", "https://"):
        assert origin(bad) is None, bad
    it = {"login": {"uris": [{"uri": "https://a.com"}, {"uri": "http://b.com"},
                             {"uri": "a.com/x"}, {"uri": None}]}}
    assert origins(it) == ["https://a.com:443"]
