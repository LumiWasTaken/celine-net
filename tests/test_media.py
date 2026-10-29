"""Media files and image captions (kb/media*.py). Tiny fixtures, a fake vision model,
no network, no real data."""
from __future__ import annotations

import base64
import hashlib
import sqlite3
from chat_fixtures import until_sync
from types import SimpleNamespace

import pytest

from celine.kb import KBs, media, media_wa
from celine.kb.chat import ChatKB
from celine.kb.importers import Cancelled, ImportContext, Unreachable
from celine.kb.tasks import Tasks

from kb_fixtures import CTX, T0, archive, msgstore

CAR, CAT = b"\xff\xd8\xff\xe0" + b"car" * 50, b"\x89PNG\r\n\x1a\n" + b"cat" * 50


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def settings(**kw) -> media.Settings:
    return media.Settings.parse({"rate_per_minute": 0, **kw})


def seeded(tmp_path):
    """A Discord archive: a car photo sent twice, a cat photo, a pdf, a text."""
    kbs = KBs(SimpleNamespace(data_dir=tmp_path, problems=[], kb={
        "discord": {"kind": "chat", "title": "Discord", "platform": "discord"}}),
        lambda *a: None)
    kb = kbs.get("discord")
    d = media.media_dir(kb)
    d.mkdir(parents=True, exist_ok=True)
    for data in (CAR, CAT, b"%PDF-1.4 x"):
        (d / sha(data)).write_bytes(data)

    def file(name, data, ct, kind="photo"):
        return {"type": kind, "name": name, "files": [
            {"name": name, "size": 1, "content_type": ct, "sha256": sha(data)}]}
    s = kb.sink()
    pid = s.person("u:bea", display_name="Bea")
    cid = s.chat("chan1", kind="group", name="Cars")
    for i, (text, med) in enumerate([
            ("look", file("car.jpg", CAR, "image/jpeg")), ("", file("cat.png", CAT, "image/png")),
            ("again", file("car.jpg", CAR, "image/jpeg")),
            ("", file("a.pdf", b"%PDF-1.4 x", "application/pdf", "file")), ("hello", None)]):
        s.message(cid, f"m{i}", ts=T0 + i * 60_000, text=text, sender=pid,
                  kind="media" if med else "text", media=med)
    s.close()
    return kbs, kb


class FakeEngine(media.Engine):
    """Describes by content; raises on images containing `bad`; `waiting`: no model."""

    def __init__(self, *, bad: bytes | None = None, waiting: str | None = None):
        super().__init__(SimpleNamespace(), llm=SimpleNamespace())
        self.calls, self.bad, self.waiting = [], bad, waiting

    def caption(self, data, mime):
        self.calls.append(mime)
        if self.waiting:
            raise media.Waiting(self.waiting)
        if self.bad and self.bad in data:
            raise RecursionError("maximum recursion depth exceeded")
        car = b"car" in data
        return {"caption": "a red car parked on a street" if car else "a grey cat on a sofa",
                "visible_text": "STOP" if car else "", "model": "far-vision", "endpoint": "far"}


def test_every_image_is_captioned_once_and_becomes_searchable(tmp_path):
    kbs, kb = seeded(tmp_path)
    assert kbs.search("sofa")["total"] == 0
    eng = FakeEngine()
    res = media.run(kb, settings(), eng, CTX)
    assert (res["done"], res["failed"], len(eng.calls)) == (2, 0, 2), "one call per file"
    got = media.caption_of(kb, [sha(CAR)])[sha(CAR)]
    assert (got["caption"], got["visible_text"]) == ("a red car parked on a street", "STOP")
    assert media.run(kb, settings(), FakeEngine(), CTX)["done"] == 0, "nothing twice"
    assert [h["ref"] for h in kbs.search("sofa")["hits"]] == ["discord:2"]
    assert kbs.search("stop")["total"] == 2, "visible text, on both messages of the car"


def test_one_bad_image_leaves_the_task_done_with_one_failed(tmp_path, monkeypatch):
    _, kb = seeded(tmp_path)
    monkeypatch.setattr(media, "Engine", lambda cfg, **kw: FakeEngine(bad=b"cat"))
    task = media.start(SimpleNamespace(tasks=Tasks(lambda ev: None), llm=None), kb,
                       SimpleNamespace(media={"rate_per_minute": 0}))
    until_sync(lambda: task.state != "running", 10)
    assert task.state == "done" and (task.result["done"], task.result["failed"]) == (1, 1)
    row = media.caption_of(kb, [sha(CAT)])[sha(CAT)]
    assert row["state"] == "failed" and row["caption"] is None and "RecursionError" in row["error"]
    again = FakeEngine()
    assert media.run(kb, settings(), again, CTX)["failed"] == 0 and again.calls == [], \
        "a failed image is tried once"


def test_no_model_marks_nothing_and_a_cancel_keeps_what_was_done(tmp_path):
    _, kb = seeded(tmp_path)
    s = settings()
    with pytest.raises(Unreachable):
        media.run(kb, s, FakeEngine(waiting="No model is set for photo descriptions."), CTX)
    assert media.caption_of(kb, [sha(CAR), sha(CAT)]) == {}, "no failed rows"
    stop = ImportContext(progress=lambda *a: None,
                         cancelled=lambda: media.stats(kb, s)["captions"]["done"] >= 1)
    with pytest.raises(Cancelled):
        media.run(kb, s, FakeEngine(), stop)
    assert media.stats(kb, s)["captions"]["done"] == 1


def test_whatsapp_folder_scan_copies_her_files_once_and_downloads_are_off(tmp_path):
    db = msgstore(tmp_path / "m.db")
    c = sqlite3.connect(db)
    c.execute("ALTER TABLE message_media ADD COLUMN file_hash TEXT")
    c.execute("UPDATE message_media SET file_hash = ? WHERE message_row_id = 8",
              (base64.b64encode(hashlib.sha256(CAR).digest()).decode(),))
    c.commit()
    c.close()
    kb = ChatKB(id="whatsapp", title="w", platform="whatsapp", path=tmp_path / "kb" / "w.db")
    archive(kb)
    folder = tmp_path / "Media"
    photo = folder / "WhatsApp Images" / "renamed.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(CAR)
    (folder / "VID-20200101-WA0001.mp4").write_bytes(b"\x00video")
    rep = media_wa.scan_folder(kb, folder, msgstore=db)
    assert rep["dry_run"] and rep["matched"]["hash"] == 1 and rep["unmatched"] == 1
    assert not media.media_dir(kb).exists(), "a dry run changes nothing"
    rep = media_wa.scan_folder(kb, folder, dry_run=False, msgstore=db)
    assert rep["messages_linked"] == 1 and rep["copied"] == 1
    assert (media.media_dir(kb) / sha(CAR)).read_bytes() == CAR
    assert photo.read_bytes() == CAR, "its folder is only read"
    again = media_wa.scan_folder(kb, folder, dry_run=False, msgstore=db)
    assert again["messages_linked"] == 0 and again["copied"] == 0
    off = media.Settings.parse({})
    assert not off.wa_download and not off.wa_media_retry
    with pytest.raises(media_wa.MediaOff):
        media_wa.fetch_via_link(kb, off, None)
    with pytest.raises(media_wa.MediaOff):
        media_wa.fetch_via_link(kb, media.Settings.parse({"wa_download": True}), None,
                                retry=True)


def test_a_download_names_its_size_and_a_refused_one_says_why(tmp_path):
    """wa-link refuses a file over its cap before downloading it, by the size the
    message names: the app sends that size and keeps wa-link's reason."""
    sent = []

    class Link:
        def raw_sync(self, method, path, body):
            sent.append(body)
            return SimpleNamespace(status_code=413, content=b"", text="", json=lambda: {
                "error": "The file is larger than the connection downloads (100 MB)."})
    kb = ChatKB(id="whatsapp", title="w", platform="whatsapp", path=tmp_path / "w.db")
    m = {"id": 1, "direct_path": "/v/x", "media_key": "a2V5", "file_hash": None,
         "enc_file_hash": None, "mtype": "document", "mime": None, "file_size": 2 << 30}
    assert media_wa._download(kb, Link(), m) == (
        "failed", "The file is larger than the connection downloads (100 MB).")
    assert sent[0]["file_length"] == 2 << 30
