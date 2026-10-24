"""Every /api answer that is an error has the app's shape {"error": {code, message}}:
an unknown path with any method, a wrong method, a bad body and a crash."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from celine import __main__ as main, boot
from celine.api import origin as origin_mod
from celine.errors import install_error_handler


def test_an_unknown_api_path_is_not_found_with_every_method():
    cfg = boot.load()
    base = f"http://localhost:{cfg.port}"
    c = TestClient(main.build_app(cfg, dev=False), base_url=base, headers={"origin": base})
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        r = c.request(method, "/api/nothing-here")
        assert r.status_code == 404, method
        assert r.json() == {"error": {"code": "not_found", "message": "/api/nothing-here"}}
    origin_mod.configure({}, False)


def test_a_wrong_method_and_a_crash_keep_the_shape():
    app = FastAPI()
    install_error_handler(app)

    @app.get("/x")
    async def x():
        raise RuntimeError("boom")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/x")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal",
                                  "message": "The server failed: RuntimeError: boom"}}
    r = c.post("/x")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
