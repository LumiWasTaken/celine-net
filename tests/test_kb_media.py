"""Edit history and downloaded attachments over HTTP. Fixtures only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from celine.api import kb as api
from celine.core.services import Services
from celine.errors import CelineError
from celine.kb.chat import ChatKB


def test_a_message_keeps_its_versions_and_serves_only_its_files_safely(tmp_path: Path):
    kb = ChatKB(id="discord", title="Discord", platform="discord",
                path=tmp_path / "kb" / "discord.db", options={})
    files = [("x.bin", "image/png", b"png"), ("x.png", "text/html", b"<script>1</script>"),
             (None, "image/svg+xml", b"<svg/>")]
    (kb.path.parent / "discord.media").mkdir(parents=True)
    for _, _, data in files + [(None, None, b"not in the message")]:
        (kb.path.parent / "discord.media" / hashlib.sha256(data).hexdigest()).write_bytes(data)
    media = {"type": "photo", "files": [{"name": n, "size": len(d), "content_type": t,
                                         "sha256": hashlib.sha256(d).hexdigest()}
                                        for n, t, d in files]}

    def seed(conn):
        conn.execute("INSERT INTO chats (id, key) VALUES (1, 'c')")
        conn.execute("INSERT INTO messages (id, chat_id, key, ts, text, kind, media) "
                     "VALUES (1, 1, 'm', 5, 'new', 'media', ?)", (json.dumps(media),))
        conn.execute("INSERT INTO message_edits (message_id, ts, old_text) VALUES (1, 3, 'older')")
        conn.execute("INSERT INTO message_edits (message_id, ts, old_text) VALUES (1, 1, 'oldest')")
    kb.file.write_sync(seed)

    app = FastAPI()
    app.state.services = Services()
    app.include_router(api.router)

    @app.exception_handler(CelineError)
    async def err(request, e):
        return JSONResponse({"error": str(e)}, status_code=e.status)
    app.state.services.kbs = SimpleNamespace(get=lambda kid: kb if kid == "discord" else None)
    c = TestClient(app)
    photo, page, vector = [hashlib.sha256(d).hexdigest() for _, _, d in files]
    assert c.get("/api/sources/discord/messages/1").json()["edits"] == [
        {"ts": 1, "text": "oldest"}, {"ts": 3, "text": "older"}], "earlier versions, oldest first"
    url = "/api/sources/discord/messages/1/media/"
    r = c.get(url + photo)                          # the stored type, not the name's
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    r = c.get(url + page)                           # a picture's name does not make it one
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert c.get(url + vector).headers["content-type"] == "application/octet-stream"
    other = hashlib.sha256(b"not in the message").hexdigest()
    assert c.get(url + other).status_code == 404
    assert c.get(f"/api/sources/discord/messages/2/media/{photo}").status_code == 404
    assert c.get(url + "..%2F..%2Fdiscord.db").status_code == 404
