"""turns/reply.py: its stored reply. The agent's words stay; only citation tokens are touched, in
the order the module docstring lists: tool names marked, web and kb spellings made
canonical, a wrong one marked |invalid, a good web one turned into its link."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from celine.turns import reply

pytestmark = pytest.mark.anyio

SHOWN = [{"role": "tool", "content": "> [whatsapp:1] Alex: we leave at nine\n[web:3] A page\n  https://x.org/a"}]


class Store:
    def __init__(self):
        self.events = []

    async def append_event(self, cid, kind, data):
        self.events.append((kind, data))


class KBs:
    def citable(self):
        return {"whatsapp"}

    def resolve_refs(self, refs):
        return {r: {"text": "we leave at nine", "sender": "Alex"} for r in refs}


def cfg():
    from celine.settings import general
    return SimpleNamespace(user="Joe", r=lambda key: general.value({}, key))


async def test_the_chain_in_order():
    store = Store()
    text = ('Alex said "we leave at nine" [whatsapp:1], later [[whatsapp:2-4]] and '
            '[web:3] plus [[web:9]] see [[chat_archive__look_up: x]] - fine.')
    out = await reply.stored(store, cfg(), "c1", "m1", text, SHOWN, [], KBs())
    assert out == ('Alex said "we leave at nine" [[whatsapp:1]], later [[whatsapp:2|invalid]] and '
                   '[x.org](https://x.org/a) plus [[web:9|invalid]] see '
                   '[[chat_archive__look_up|invalid]] - fine.')
    [(kind, data)] = store.events
    assert kind == "citations.checked" and data["refs"] == 4
    assert {p["ref"] for p in data["invalid"]} == {"whatsapp:2", "web:9"}


async def test_without_knowledge_bases_only_the_web_links_are_made():
    store = Store()
    out = await reply.stored(store, cfg(), "c1", "m1", "see [web:3] and [whatsapp:1]", SHOWN, [], None)
    assert out == "see [x.org](https://x.org/a) and [whatsapp:1]" and store.events == []


async def test_plain_text_is_returned_as_it_is():
    assert await reply.stored(Store(), cfg(), "c", "m", "no marks here", [], [], KBs()) == "no marks here"
    assert await reply.stored(Store(), cfg(), "c", "m", None, [], [], KBs()) == ""
