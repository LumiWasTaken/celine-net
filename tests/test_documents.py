"""Documents: files of any kind, its titles and notes on them, search, descriptions,
Celine's tools, the API. Tiny generated files, fake models."""
from __future__ import annotations

from chat_fixtures import until_sync
from pathlib import Path
from types import SimpleNamespace

import pytest

from celine.boot import load
from celine.core.services import Services
from celine.kb import KBs
from celine.kb import documents_describe as KD
from celine.kb.documents import DocumentsKB, DocumentsPathError
from celine.kb.importers import Cancelled, ImportContext


def ctx(cancel_after: int | None = None):
    n = [0]

    def cancelled():
        n[0] += 1
        return cancel_after is not None and n[0] > cancel_after
    return ImportContext(progress=lambda *a: None, cancelled=cancelled)


def mk(tmp_path: Path, options: dict | None = None, **kw) -> DocumentsKB:
    kb = DocumentsKB(id="documents", title="Documents", root=tmp_path / "files",
                     path=tmp_path / "kb" / "documents.db",
                     options={"chunk_over_kb": 0, **(options or {})}, **kw)
    kb.root.mkdir(parents=True, exist_ok=True)
    return kb


def read_all(kb: DocumentsKB) -> None:
    kb.sync()
    kb.process(ctx())


class FakeModel(list):
    def __call__(self, system: str, user: str) -> tuple[str, str]:
        self.append(system)
        return "A German travel diary about a train journey.", "fake-9b"


def _kbs(tmp_path):
    from celine.settings import general
    kbs = KBs(SimpleNamespace(data_dir=tmp_path, problems=[], kb={
        "documents": {"kind": "documents", "title": "Documents", "chunk_over_kb": 0}},
        r=lambda key: general.value({}, key)), lambda *a: None)
    kbs.documents().describer = FakeModel()
    return kbs


def wait_idle(kbs, timeout=15.0):
    until_sync(lambda: not kbs.tasks.running("documents", "documents")
               and not kbs.tasks.running("documents", "describe"), timeout)


def test_edits_and_moves_keep_her_titles_and_notes(tmp_path):
    kb = mk(tmp_path)
    (kb.root / "cars").mkdir()
    f = kb.root / "cars" / "celine.md"
    f.write_text("# Celine\n\nDer Mast ist neu.\n\nZweiter Absatz über Segeln.")
    (kb.root / "doc.txt").write_text("Die Straße nach Zürich.")
    read_all(kb)
    it = kb.find_item("cars/celine.md")
    mast = next(s for s in kb.segments(it["id"])["segments"] if "Mast" in s["text"])
    kb.set_meta(it["id"], title="Mein Boot", description="Notizen zur Celine")
    assert [g["item"]["path"] for g in kb.search_segments("strasse")["items"]] == ["doc.txt"]
    assert kb.search_segments("notizen")["items"][0]["item"]["id"] == it["id"]
    f.write_text("# Celine\n\nDer Mast ist neu.\n\nNeuer zweiter Absatz.")   # another size
    read_all(kb)
    segs = kb.segments(it["id"])["segments"]
    assert next(s for s in segs if "Mast" in s["text"])["id"] == mast["id"], \
        "an unchanged paragraph keeps its id (and its citations)"
    assert kb.search_segments("segeln")["items"] == []
    (kb.root / "archive").mkdir()
    f.rename(kb.root / "archive" / "celine.md")         # moved outside the app
    read_all(kb)
    moved = kb.find_item("archive/celine.md")
    assert (moved["id"], moved["title"], moved["user_description"]) == (
        it["id"], "Mein Boot", "Notizen zur Celine")


def test_adding_never_overwrites_and_stays_inside_the_folder(tmp_path):
    kb = mk(tmp_path)
    src = tmp_path / "up.txt"
    src.write_text("hello knowledge")
    a = kb.add_file(src, "note.txt", folder="Inbox", title="T")
    src.write_text("hello knowledge")
    b = kb.add_file(src, "again.txt")
    assert b["existing"] and b["item"]["id"] == a["item"]["id"], "the same file once"
    src.write_text("other")
    assert kb.add_file(src, "note.txt", folder="Inbox")["item"]["path"] == "Inbox/note (2).txt"
    assert (kb.root / "Inbox" / "note.txt").read_text() == "hello knowledge"
    src.write_text("x")
    with pytest.raises(DocumentsPathError):
        kb.add_file(src, "a.txt", folder="../outside")
    for bad in ("../escape.md", ".hidden/x.md", "."):
        with pytest.raises(DocumentsPathError):
            kb.write(bad, "x")
    assert not (tmp_path / "outside").exists() and not (tmp_path / "escape.md").exists()
    for bad in ({"name": "../b.md"}, {"name": ".."}, {"folder": ".."}):
        with pytest.raises(DocumentsPathError):
            kb.move_item(a["item"]["id"], **bad)
    with pytest.raises(DocumentsPathError):
        kb.make_folder("../x")


def test_descriptions_resume_mid_file_and_wait_when_no_model_is_free(tmp_path):
    fake = FakeModel()
    kb = mk(tmp_path, {"describe_part_chars": 300, "describe_max_parts": 6}, describer=fake)
    (kb.root / "book.txt").write_text("\n\n".join("Seite " + "wort " * 50 for _ in range(10)))
    read_all(kb)
    with pytest.raises(Cancelled):
        KD.run(kb, ctx(cancel_after=4))
    assert 0 < len(fake) < 7
    KD.run(kb, ctx())
    assert sum(s in (KD._p("describe_part"), KD._p("describe_part_late")) for s in fake) == 6, \
        "no part summarised twice"
    assert kb.find_item("book.txt")["ai"]["state"] == "done"
    assert kb.search_segments("travel diary")["total_items"] == 1

    def busy(system, user):
        raise KD.Soft("no model free: busy")
    kb.describer = busy
    (kb.root / "book.txt").write_text("Jetzt ein anderes Thema.")
    read_all(kb)
    for _ in range(3):
        assert KD.run(kb, ctx())["stopped"]
    assert kb.find_item("book.txt")["ai"]["state"] == "pending", "it waits, it never fails"


@pytest.mark.anyio
async def test_celines_tools_search_and_read_on(tmp_path, monkeypatch):
    from celine.core.services import Services
    from celine.skills import ToolContext
    from celine.skills.documents import UserDocuments

    async def emit(_):
        return None
    tc = ToolContext(conversation_id="c", turn_id="t", tool_call_id="x", store=None,
                     config=None, emit=emit)
    kbs = _kbs(tmp_path)
    kb = kbs.documents()
    (kb.root / "reise.txt").write_text("\n\n".join(
        f"Tag {i}: wir fahren nach Minsk und essen Blini." + " mehr" * 80 for i in range(60)))
    kb.rescan()
    wait_idle(kbs)
    sk = UserDocuments(SimpleNamespace(user="User"))
    sk.app = Services(kbs=kbs)
    assert "[documents:" in (await sk.search(tc, query="minsk blini")).content
    r = await sk.read(tc, document="reise", max_tokens=600)
    later = r.display["later"]
    r2 = await sk.read(tc, ref=later, max_tokens=600)
    assert r2.display["from"] == int(later.split(":")[1]), "reading goes on where it stopped"
    assert (await sk.read(tc, document="does not exist")).is_error
    before = await sk.read(tc, document="reise", direction="before", max_tokens=600)
    assert "Tag 0:" in before.content and "None" not in before.content
    from celine.kb import semantic

    def broken(*_a, **_k):
        raise semantic.SemanticError("the embedding server refused")
    monkeypatch.setattr(type(kb), "semantic_hits", broken)
    out = (await sk.search(tc, query="minsk blini")).content
    assert ("Semantic topic search failed (the embedding server refused); these are the "
            "word search hits.") in out and "[documents:" in out


def test_the_largest_upload_is_an_advanced_setting(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from celine.api import documents as api
    from celine.config import ROOT
    from celine.errors import install_error_handler
    from celine.kb import SOURCE_KEYS
    assert "max_upload_mb" not in SOURCE_KEYS["documents"]
    assert "max_upload_mb" not in (ROOT / "config" / "kb.toml").read_text(), "one source"
    app = FastAPI()
    cfg = load()
    assert cfg.r("max_upload_mb") == 512
    cfg.settings.general["max_upload_mb"] = 1
    app.state.services = Services(cfg=cfg)
    install_error_handler(app)
    app.include_router(api.router)
    app.state.services.kbs = _kbs(tmp_path)
    app.state.services.store = SimpleNamespace(emit_global=lambda ev: None)
    r = TestClient(app).post("/api/documents", files=[
        ("files", ("big.txt", b"a" * (1024 * 1024 + 1), "text/plain"))]).json()
    assert r["items"] == [] and r["rejected"] == [{
        "name": "big.txt", "reason": "The file is larger than 1 MB, the Largest upload in "
                                     "Settings, Advanced."}]


def test_api_contract(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from celine.api import bulk, documents as api
    from celine.errors import install_error_handler
    kbs = _kbs(tmp_path)
    app = FastAPI()
    app.state.services = Services(cfg=load())
    install_error_handler(app)
    app.include_router(api.router)
    app.include_router(bulk.router)
    app.state.services.kbs = kbs
    app.state.services.store = SimpleNamespace(emit_global=lambda ev: None)
    c = TestClient(app)
    r = c.post("/api/documents", data={"folder": "Reisen"},
               files=[("files", ("tagebuch.txt", b"Wir fahren nach Minsk.", "text/plain")),
                      ("files", ("same.txt", b"Wir fahren nach Minsk.", "text/plain")),
                      ("files", ("empty.txt", b"", "text/plain"))]).json()
    assert [i["path"] for i in r["items"]] == ["Reisen/tagebuch.txt"]
    assert r["existing"][0]["name"] == "same.txt" and r["rejected"][0]["name"] == "empty.txt"
    wait_idle(kbs)
    iid = r["items"][0]["id"]
    assert c.post("/api/documents/folders", json={"path": "Archiv"}).status_code == 200
    it = c.patch(f"/api/documents/{iid}", json={"title": "Reise", "name": "reise.txt",
                                                      "folder": "Archiv"}).json()
    assert (it["title"], it["path"], it["id"]) == ("Reise", "Archiv/reise.txt", iid)
    got = c.get(f"/api/documents/{iid}/file")
    assert got.content == b"Wir fahren nach Minsk."
    assert got.headers["cache-control"] == "no-cache"  # a save shows at once
    bad = c.post("/api/documents", data={"folder": "../x"},
                 files=[("files", ("a.txt", b"a", "text/plain"))])
    assert bad.status_code == 400
    assert c.delete(f"/api/documents/{iid}").json()["deleted"] == 1
    assert c.get(f"/api/documents/{iid}").json()["error"]["code"] == "not_found"
    wait_idle(kbs)


def test_the_drawer_folders_sort_hide_and_move_keeping_ids(tmp_path):
    events = []
    kb = mk(tmp_path, tasks=SimpleNamespace(emit=events.append))
    (kb.root / "stories" / "drafts").mkdir(parents=True)
    (kb.root / "stories" / "lake.md").write_text("# The lake\n\nWater.")
    (kb.root / "stories" / "drafts" / "fox.md").write_text("# Fox\n\n" + "run " * 300)
    (kb.root / "song.mp3").write_bytes(b"ID3" + bytes(range(256)) * 8)
    (kb.root / "empty").mkdir()
    read_all(kb)
    top = kb.folders_in("")
    assert [(f["name"], f["items"]) for f in top] == [("empty", 0), ("stories", 2)]
    assert [f["name"] for f in kb.folders_in("", sort="size")] == ["stories", "empty"]
    assert [i["name"] for i in kb.list_items(folder="", inside=True)["items"]] == ["song.mp3"]
    song = kb.find_item("song.mp3")
    assert song["kind"] == "file", "any file is kept and listed"
    assert [i["name"] for i in kb.list_items(folder="stories", inside=True)["items"]] \
        == ["lake.md"]
    fox = kb.find_item("fox.md")
    events.clear()
    kb.move_folder("stories/drafts", name="old", folder="")
    assert kb.find_item("old/fox.md")["id"] == fox["id"], "a moved folder keeps its ids"
    assert {e["path"] for e in events if e["type"] == "documents.changed"} \
        >= {"", "stories", "old"}
    assert kb.search_segments("fox")["items"]
    kb.trash_folder("old/./", True)            # kept as "old", as the listings read it
    assert [f["name"] for f in kb.folders_in("")] == ["empty", "stories"]
    assert [f["name"] for f in kb.folders_in("", trashed=True)] == ["empty", "old", "stories"]
    assert not kb.search_segments("fox")["items"], "the trash is out of search too"
    assert kb.find_item("old/fox.md") is None and kb.find_item(str(fox["id"])) is None
    assert [f["path"] for f in kb.trash()["folders"]] == ["old"] and kb.trash()["items"] == []
    with pytest.raises(DocumentsPathError):
        kb.set_trashed(fox["id"], False)        # it comes back with its folder
    kb.trash_folder("old", False)
    assert kb.search_segments("fox")["items"], "back with the folder"
    kb.set_trashed(fox["id"], True)
    assert kb.list_items(folder="old", inside=True)["total"] == 0
    assert [i["id"] for i in kb.trash()["items"]] == [fox["id"]]
    with pytest.raises(DocumentsPathError):
        kb.move_folder("stories", folder="stories")
    kb.move_folder("old", folder="stories/./")
    assert kb.item(fox["id"])["path"] == "stories/old/fox.md"
    assert [f["name"] for f in kb.folders_in("stories")] == ["old"]


def test_she_writes_into_the_drawer_and_never_over_their_files(tmp_path):
    events = []
    kb = mk(tmp_path, tasks=SimpleNamespace(emit=events.append))
    (kb.root / "mine.md").write_text("the user's")
    read_all(kb)
    events.clear()
    out = kb.write("reports/week", "# Week\n\nAll good.", author="celine")
    assert out["path"] == "reports/week.md" and out["item"]["author"] == "celine"
    changed = [e["path"] for e in events if e["type"] == "documents.changed"]
    assert sorted(changed) == ["", "reports"], "once per folder"
    kb.write("reports/week.md", "# Week\n\nBetter.", author="celine")
    with pytest.raises(DocumentsPathError):
        kb.write("mine.md", "hers now", author="celine")
    assert (kb.root / "mine.md").read_text() == "the user's"
    # the user edits its file outside the app: it is theirs now
    (kb.root / "reports" / "week.md").write_text("# Week\n\nMy own words, longer.")
    with pytest.raises(DocumentsPathError):
        kb.write("reports/week.md", "# Week\n\nMine again.", author="celine")
    assert kb.find_item("reports/week.md")["author"] == "user"


def test_api_folders_pages_and_folder_uploads(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from celine.api import bulk, documents as api
    from celine.errors import install_error_handler
    kbs = _kbs(tmp_path)
    app = FastAPI()
    app.state.services = Services(cfg=load())
    install_error_handler(app)
    app.include_router(api.router)
    app.include_router(bulk.router)
    app.state.services.kbs = kbs
    app.state.services.store = SimpleNamespace(emit_global=lambda ev: None)
    c = TestClient(app)
    r = c.post("/api/documents", data={"folder": "Up", "paths": ["trip/a.txt",
                                                                       "trip/b/c.txt"]},
               files=[("files", ("a.txt", b"one", "text/plain")),
                      ("files", ("c.txt", b"two", "text/plain"))]).json()
    assert [i["path"] for i in r["items"]] == ["Up/trip/a.txt", "Up/trip/b/c.txt"]
    wait_idle(kbs)
    page = c.get("/api/documents/folders/Up/trip").json()
    assert [f["name"] for f in page["folders"]] == ["b"]
    assert [i["name"] for i in page["items"]] == ["a.txt"]
    assert [b["path"] for b in page["breadcrumbs"]] == ["", "Up", "Up/trip"]
    assert page["folder"]["items"] == 2 and page["next"] is None
    first = c.get("/api/documents", params={"limit": 1, "sort": "name"}).json()
    rest = c.get("/api/documents", params={"limit": 1, "sort": "name",
                                                 "cursor": first["next"]}).json()
    assert [i["name"] for i in first["items"] + rest["items"]] == ["a.txt", "c.txt"]
    assert rest["next"] is None
    assert c.get("/api/documents", params={"cursor": first["next"],
                                                 "sort": "size"}).status_code == 400
    assert c.get("/api/documents/folders/nope").status_code == 404
    top = c.get("/api/documents/folders/").json()            # the top: an empty path
    assert top["folder"]["path"] == "" and [f["name"] for f in top["folders"]] == ["Up"]
    moved = c.patch("/api/documents/folders/Up/trip", json={"name": "Trip",
                                                           "trashed": True}).json()
    assert moved["path"] == "Up/Trip" and moved["trashed_at"]
    assert c.get("/api/documents/folders/Up").json()["folders"] == []
    assert [f["path"] for f in c.get("/api/documents?trashed=1").json()["folders"]] == ["Up/Trip"]
    for action in ("restore", "trash"):           # many at once: each through its route
        assert c.post("/api/bulk-actions", json={"what": "folders", "action": action,
                                                 "ids": ["Up/Trip"]}).json() == \
            {"done": ["Up/Trip"], "skipped": []}
    assert c.delete("/api/documents/folders/Up").status_code == 400, \
        "only from the trash"
    assert c.delete("/api/documents/folders/Up/Trip").json() == \
        {"deleted": 2, "path": "Up/Trip"}
    assert c.get("/api/documents?trashed=1").json() == {"folders": [], "items": []}


def test_a_file_gone_while_reading_waits_and_is_not_a_failure(tmp_path):
    kb = mk(tmp_path)
    (kb.root / "a.txt").write_text("stays")
    (kb.root / "b.txt").write_text("goes")
    kb.sync()
    (kb.root / "b.txt").unlink()
    out = kb.process(ctx())
    assert (out["read"], out["failed"]) == (1, 0)
    assert kb.find_item("b.txt")["state"] == "pending", "the next run looks again"


def test_a_scoped_search_narrows_before_the_result_cap(tmp_path):
    kb = mk(tmp_path)
    for i in range(30):
        (kb.root / f"big{i}.txt").write_text("\n\n".join(f"segel {i} {j}" for j in range(100)))
    (kb.root / "mine.txt").write_text("segel only here, once")
    read_all(kb)
    mine = kb.find_item("mine.txt")["id"]
    hits = kb.search_segments("segel", item_ids=[mine])
    assert [g["item"]["id"] for g in hits["items"]] == [mine]
