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


def _ask(a, action, conversation, why=WHY, **kw):
    return a.ask("vault", conversation, action, why=why, **kw)


async def test_approve_and_deny_reach_the_tool_and_the_card_names_the_outside_read(monkeypatch):
    a = _taps(monkeypatch)
    a.seen("c1", "browser__open")
    a.seen("c1", "notebook__read")              # not from outside: the browser stays named
    action = ap.SHELL.format(entry="example-shop")
    task = asyncio.create_task(_ask(a, action, "c1", facts=[("Entry", "example-shop")]))
    await _waits(a)
    [card] = a.view()
    assert card["facts"] == [{"label": "Entry", "value": "example-shop"},
                             {"label": "Reason", "value": WHY},
                             {"label": "Read right before", "value": "Browser: open a page"}]
    await _answer(a, True)
    assert await task is None and a.waiting == {}
    got, _ = await asyncio.gather(_ask(a, action, "c2"), _answer(a, False))
    assert got == "sam denied it; nothing was done."
    assert not a.decide("nope", True, OWNER)


async def test_silence_outside_a_turn_times_out_and_the_card_has_the_fixed_fields(monkeypatch):
    a = _taps(monkeypatch)
    task = asyncio.create_task(_ask(a, ap.ROTATE.format(entry="x"), "c1",
                                    facts=[("Sites", "None")]))
    await _waits(a)
    [card] = a.view()
    assert set(card) == {"id", "kind", "title", "facts", "detail", "help", "where", "home",
                         "link", "tool_call_id", "message_id"} and card["kind"] == "vault"
    assert card["title"] == "Celine asks to change the password of x."
    assert card["help"] == ap.HELP["vault"] and card["detail"] is None
    a.forget()
    await task
    a = _taps(monkeypatch, ask_for=0)
    got = await _ask(a, ap.ROTATE.format(entry="x"), "c1")
    assert got == "sam did not answer in 1 minute; nothing was done."


async def test_one_at_a_time_no_hourly_cap_and_denials_lock_nothing(monkeypatch):
    a = _taps(monkeypatch)
    first = asyncio.create_task(_ask(a, "a", "c"))
    await _waits(a)
    assert (await _ask(a, "b", "c")).startswith("Another request waits for sam")
    await _answer(a, True)
    assert await first is None
    for _ in range(8):                           # as many as it needs, one at a time
        got, _ = await asyncio.gather(_ask(a, "a", "c"), _answer(a, True))
        assert got is None

    a = _taps(monkeypatch)
    for _ in range(2):
        await asyncio.gather(_ask(a, "a", "c"), _answer(a, False))
    got, _ = await asyncio.gather(_ask(a, "a", "c"), _answer(a, True))
    assert got is None                           # two denials, the next one still asks


async def test_a_lock_denies_what_waits_and_no_reason_asks_nothing(monkeypatch):
    a = _taps(monkeypatch)
    waiting = asyncio.create_task(_ask(a, "a", "c"))
    await _waits(a)
    a.forget()                                   # the vault locked
    assert await waiting == "sam denied it; nothing was done."
    for why in ("", "   ", "need it"):
        got = await _ask(a, "a", "c", why=why)
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


async def test_the_agents_why_is_shown_whole(monkeypatch):
    a = _taps(monkeypatch)
    why = "the order script\n  needs to log in, " + "because " * 80
    task = asyncio.create_task(_ask(a, "a", "c", why=why))
    await _waits(a)
    assert a.view()[0]["facts"] == [{"label": "Reason", "value": why.strip()}]
    await _answer(a, True)
    assert await task is None


def test_every_help_text_is_two_sentences_at_most_and_names_nobody():
    for kind, text in ap.HELP.items():
        assert kind in ap.KINDS and text.endswith("."), kind
        assert len(text) <= 220 and text.count(". ") <= 1, kind
        assert not any(d in text for d in ("\u2014", "\u2013", " \u00b7 ")), kind
        words = text.replace(".", " ").replace(",", " ").split()
        assert not {"she", "agent", "Celine", "Tink"} & set(words), kind


def test_the_help_text_carries_the_configured_name():
    from celine.boot import load
    cfg = load()
    cfg.agent.name, cfg.agent.pronouns = "Tink", "he"
    a = ap.Approvals(cfg)
    assert a._help("agent").startswith("Tink's persona") and "Settings, Tink." in a._help("agent")
    assert "Tink proposes" in a._help("settings")
