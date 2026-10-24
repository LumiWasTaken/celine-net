"""The files web/public puts at the root of the build (the PWA manifest, its icons) are served
as files; every other path is the app's index.html. The manifest itself: standalone, opens a
where you left off, the canvas colour of the tokens, icons that exist, linked with the login cookie."""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from celine import __main__ as main, boot
from celine.api import origin as origin_mod

WEB = Path(__file__).resolve().parents[1] / "web"


def test_root_files_and_the_app(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html>app")
    (tmp_path / "manifest.webmanifest").write_text('{"name": "x"}')
    (tmp_path / "icon-192.png").write_bytes(b"\x89PNG")
    (tmp_path / "icon.svg").write_text("<svg/>")
    (tmp_path / "favicon.ico").write_bytes(b"ico")
    (tmp_path / "theme.js").write_text("try {} catch (e) {}")
    (tmp_path / "secret.txt").write_text("no")
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    cfg = boot.load()
    base = f"http://localhost:{cfg.port}"
    c = TestClient(main.build_app(cfg, dev=False), base_url=base, headers={"origin": base})
    assert c.get("/manifest.webmanifest").text == '{"name": "x"}'
    assert c.get("/icon-192.png").content == b"\x89PNG"
    assert c.get("/icon.svg").text == "<svg/>"
    assert c.get("/favicon.ico").content == b"ico"
    assert c.get("/theme.js").text == "try {} catch (e) {}"
    for path in ("/secret.txt", "/routines"):
        assert c.get(path).text == "<!doctype html>app", path
    # encoded dots, so the client cannot collapse them before the app sees the path
    for path in ("/assets/%2e%2e/secret.txt", "/%2e%2e/secret.txt"):
        r = c.get(path)
        assert r.status_code in (200, 404) and r.text != "no", path
    origin_mod.configure({}, False)


def test_the_manifest():
    m = json.loads((WEB / "public" / "manifest.webmanifest").read_text())
    canvas = re.search(r"--bg-canvas:[^\n]*?(#[0-9A-Fa-f]{6})", (WEB / "src" / "styles" / "tokens.css").read_text())[1]
    assert m["display"] == "standalone"
    assert m["theme_color"].lower() == m["background_color"].lower() == canvas.lower()
    assert {"192x192", "512x512"} <= {i["sizes"] for i in m["icons"]}
    for icon in m["icons"]:
        assert (WEB / "public" / icon["src"].lstrip("/")).is_file(), icon["src"]
        assert Path(icon["src"]).suffix in main._ROOT_FILES
    html = (WEB / "index.html").read_text()
    assert re.search(r'<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials"', html)
    assert f'name="theme-color" content="{canvas}"' in html
    # the look kept on this device, before the first paint (a file: the CSP allows no inline script)
    assert '<script src="/theme.js"></script>' in html and (WEB / "public" / "theme.js").is_file()
