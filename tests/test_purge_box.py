"""Delete permanently, only from the Trash: terminals and artifacts. A fake shellbox."""
from __future__ import annotations

import pytest

from celine.terminals import store as terminals
from test_terminals_api import Daemon, _app, _client, term

pytestmark = pytest.mark.anyio


async def test_a_terminal_is_deleted_for_good_only_from_the_trash(store):
    daemon = Daemon(term("build", "ab12cd"))
    app = _app(store, daemon)
    sync = app.state.services.terminals
    await sync.refresh()
    raw = terminals.log_path(store.db_path.parent, "ab12cd")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"ls\n")
    async with _client(app) as client:
        r = await client.delete("/api/terminals/ab12cd", params={"for_good": 1})
        assert r.status_code == 409 and r.json()["error"]["message"] == "It is not in the Trash."
        daemon.terminals.clear()
        await sync.refresh()                         # gone from the daemon: it ended
        assert (await client.delete("/api/terminals/ab12cd")).status_code == 200
        assert (await client.delete("/api/terminals/ab12cd",
                                    params={"for_good": 1})).json() == {"ok": True}
        assert await terminals.get(store, "ab12cd") is None and not raw.exists()


async def test_an_artifact_is_deleted_for_good_only_from_the_trash(store):
    from celine.artifacts import store as artifacts
    from celine.conversations import store as conversations
    from test_artifacts import _app as art_app, _client as art_client, _write

    cid = await conversations.create(store)
    out = await _write(store, cid, title="Budget", content="<h1>Budget</h1>")
    aid = out.display["artifact"]["id"]
    async with art_client(art_app(store)) as c:
        r = await c.delete(f"/api/artifacts/{aid}", params={"for_good": 1})
        assert r.status_code == 409 and r.json()["error"]["message"] == "It is not in the Trash."
        await c.delete(f"/api/artifacts/{aid}")
        assert (await c.delete(f"/api/artifacts/{aid}", params={"for_good": 1})).json() == \
            {"ok": True}
        assert await artifacts.get(store, aid) is None
        assert (await c.get(f"/api/artifacts/{aid}")).status_code == 404
