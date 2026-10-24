"""A note's History over HTTP, in the shape of every long text's History: every change
keeps the text it had in .versions, the newest is the text it has now, each with the
line that changed; Restore this version is the editor's, so only Save writes."""
from __future__ import annotations

import pytest

from celine import boot
from celine.api import notebook as notebook_api
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


async def test_a_notes_history_lists_every_text_newest_first_with_what_changed(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(notebook_api.router)
    async with client(app) as c:
        put = lambda text: c.put("/api/notebook/entries/lore/sea.md", json={"content": text})
        hist = lambda **q: c.get("/api/notebook/versions", params={"path": "lore/sea.md", **q})
        assert (await put("# sea\nwaves")).status_code == 200
        assert (await put("# sea\nwaves\ngulls")).status_code == 200
        assert (await put("# sea\ngulls")).status_code == 200
        got = (await hist()).json()
        assert got["total"] == 3 and got["next"] is None
        assert (got["current"]["id"], got["current"]["change"]) == (3, "waves"), \
            "the text it has now; a line taken out is the change when nothing was added"
        assert [(v["id"], v["change"]) for v in got["versions"]] == [(2, "gulls"), (1, "sea")], "the texts before, newest first"
        assert all(v["at"].endswith("+00:00") and v["by"] == "" for v in [got["current"], *got["versions"]])
        found = (await hist(q="GULL")).json()
        assert found["current"] is None and [v["id"] for v in found["versions"]] == [2]
        assert [v["id"] for v in (await hist(before=2)).json()["versions"]] == [1]
        old = (await c.get("/api/notebook/versions/2", params={"path": "lore/sea.md"})).json()
        assert old["text"].endswith("# sea\nwaves\ngulls") and old["change"] == "gulls"
        assert (await c.get("/api/notebook/versions/9", params={"path": "lore/sea.md"})).status_code == 404
        # shown as it reads it, stored as written; the History keeps it as written
        r = await put("I am {{name}}, {{username}}'s.")
        assert r.json()["content"].endswith("I am {{name}}, {{username}}'s.")
        got = (await c.get("/api/notebook/entries/lore/sea.md")).json()
        assert got["content"].endswith("{{username}}'s.")
        assert got["shown"].endswith(f"I am {cfg.name}, {cfg.user}'s.")
        newest = (await c.get("/api/notebook/versions/4", params={"path": "lore/sea.md"})).json()
        assert newest["text"].endswith("{{username}}'s.")
        lore = next(n for n in (await c.get("/api/notebook/tree")).json()["tree"] if n["path"] == "lore")
        sea = lore["children"][0]
        assert sea["updated_at"].endswith("+00:00") and sea["line"] is None, "no summary yet: no line"


async def test_a_move_takes_the_history_along_a_delete_keeps_a_copy_and_a_purge_ends_it(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(notebook_api.router)
    root = cfg.her.notebook
    async with client(app) as c:
        for text in ("first", "second"):
            await c.put("/api/notebook/entries/lore/sea.md", json={"content": text})
        assert (await c.patch("/api/notebook/entries/lore",
                              json={"path": "world"})).status_code == 200
        moved = (await c.get("/api/notebook/versions", params={"path": "world/sea.md"})).json()
        assert moved["current"]["change"] == "second" and [v["change"] for v in moved["versions"]] == ["first"]
        assert (await c.delete("/api/notebook/entries/world")).status_code == 200
        assert not (root / "world").exists()
        kept = [p.read_text() for p in (root / ".trash").rglob("sea.md")]
        assert len(kept) == 1 and kept[0].endswith("second")
        trash = (await c.get("/api/notebook/trash")).json()["trash"]
        assert [(t["path"], t["kind"]) for t in trash] == [("world/sea.md", "file")], \
            "a folder with one note lists as the note: it restores to the same place"
        r = await c.post(f"/api/notebook/trash/{trash[0]['id']}/restore")
        assert r.json() == {"ok": True, "path": "world/sea.md"}
        assert (root / "world/sea.md").read_text().endswith("second")
        assert (await c.get("/api/notebook/trash")).json()["trash"] == []
        assert (await c.post("/api/notebook/trash/x/restore")).status_code == 404
        # deleted again, the user purges it from the trash
        await c.delete("/api/notebook/entries/world/sea.md")
        [entry] = (await c.get("/api/notebook/trash")).json()["trash"]
        assert (await c.delete(f"/api/notebook/trash/{entry['id']}")).json() == \
            {"ok": True, "path": "world/sea.md"}
        assert not (root / ".trash" / entry["id"]).exists()
        assert (await c.delete(f"/api/notebook/trash/{entry['id']}")).status_code == 404
