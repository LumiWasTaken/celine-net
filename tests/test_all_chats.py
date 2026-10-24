"""All chats: every chat source's chats as one list, in the order asked for."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine.kb.chat import ChatKB


def test_all_chats_merges_the_sources_in_order(tmp_path):
    from celine.api import kb as api_mod
    kbs = []
    for kid, chats in (("whatsapp", {"Ana": 3, "Cleo": 1}), ("discord", {"Bea": 2})):
        kb = ChatKB(id=kid, title=kid, platform=kid, path=tmp_path / f"{kid}.db")
        sink = kb.sink()
        for name, n in chats.items():
            cid = sink.chat(f"k-{name}", kind="group", name=name)
            for i in range(n):  # the more messages, the later the last one
                sink.message(cid, f"{name}-{i}", ts=1_758_700_000_000 + (n * 10 + i) * 60_000, text="hi")
        sink.close()
        kbs.append(kb)
    app = FastAPI()
    app.state.services = SimpleNamespace(kbs=SimpleNamespace(chats=lambda: kbs))
    app.include_router(api_mod.router)
    c = TestClient(app)

    def names(**p):
        r = c.get("/api/archive/chats", params=p).json()
        return [x["name"] for x in r["chats"]], r["total"]
    assert names() == (["Ana", "Bea", "Cleo"], 3)
    assert names(sort="name") == (["Ana", "Bea", "Cleo"], 3)
    assert names(sort="count", offset=1, limit=1) == (["Bea"], 3)
    assert names(q="e") == (["Bea", "Cleo"], 2)
    assert {x["kb"] for x in c.get("/api/archive/chats").json()["chats"]} == {"whatsapp", "discord"}
