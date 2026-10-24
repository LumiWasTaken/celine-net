"""First messages of different conversations are not versions of each other."""
from __future__ import annotations

from chat_fixtures import user_says

import pytest

from celine.conversations import store as conversations, tree


@pytest.mark.anyio
async def test_root_siblings_stay_in_their_conversation(store):
    ids = []
    for n in range(3):
        cid = await conversations.create(store, f"c{n}")
        mid, _, _ = await user_says(store, cid, f"hello {n}")
        ids.append((cid, mid))
    cid, mid = ids[1]
    s = await tree.siblings(store, cid, mid)
    assert s["count"] == 1 and s["siblings"][0]["id"] == mid
    chain = await store.read(lambda c: tree.path(c, cid))
    assert chain[0]["siblings"] == 1
