"""The login (server/celine/auth) on the real app's routes: a software passkey, a
password and a PIN.
The app is built without its lifespan: the gate answers before any route runs."""
from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import WebSocketDenialResponse

from auth_fixtures import SoftPasskey
from celine import boot
from celine.__main__ import build_app
from celine.api import origin as origin_mod
from celine.auth import COOKIE, credentials, startup_problem, store as store_mod

ORIGIN = "http://localhost:8766"


@pytest.fixture(autouse=True)
def _fast_scrypt(monkeypatch):
    monkeypatch.setattr(credentials, "_N", 2 ** 4)        # the real cost is ~50 ms per check
    yield
    origin_mod.configure({}, False)


def make_app(**server):
    cfg = boot.load()
    cfg.server.update({"login": "on", **server})
    return build_app(cfg, dev=False)


@pytest.fixture
def app():
    return make_app()


def client(app, base=ORIGIN, address=None) -> TestClient:
    return TestClient(app, base_url=base, headers={"origin": base},
                      **({"client": (address, 5000)} if address else {}))


class Clock:
    t = 1_800_000_000

    def __call__(self) -> int:
        return self.t


def register(c: TestClient, code: str, pk: SoftPasskey, **kw):
    start = c.post("/api/auth/register/start", json={"code": code}).json()
    return c.post("/api/auth/register/finish", json={
        "ceremony": start["ceremony"], "credential": pk.create(start["options"], **kw)})


def setup_first_passkey(app, c: TestClient, pk: SoftPasskey):
    r = register(c, app.state.auth.store.new_setup_code(), pk)
    assert r.status_code == 200, r.text
    return r


def login(c: TestClient, pk: SoftPasskey):
    start = c.post("/api/auth/login/start").json()
    return c.post("/api/auth/login/finish", json={"ceremony": start["ceremony"],
                                                  "credential": pk.get(start["options"])})


def all_routes(routes, prefix: str = ""):
    """Every route of the app, through FastAPI's included routers."""
    for r in routes:
        if hasattr(r, "original_router"):
            yield from all_routes(r.original_router.routes, prefix + r.include_context.prefix)
        else:
            yield re.sub(r"\{[^}]+\}", "x", prefix + getattr(r, "path", "")), r


def test_without_a_session_every_route_and_socket_is_an_unknown_route(app):
    c = client(app)
    c.cookies.set(COOKIE, "made-up-token")
    login_open = {"/api/auth/login/start", "/api/auth/login/finish", "/api/auth/login",
                  "/api/auth/register/start", "/api/auth/register/finish",
                  "/api/auth/register/credential"}
    checked = 0
    for path, route in all_routes(app.routes):
        if isinstance(route, (APIRoute, Route)) and path.startswith("/llm/v1/"):
            # the model proxy for its shellbox checks its own key (api/llm_proxy.py)
            for method in route.methods - {"HEAD"}:
                r = c.request(method, path, headers={"Authorization": "Bearer made-up"})
                assert r.status_code in (401, 503), (method, path, r.status_code)
                assert "set-cookie" not in r.headers
        elif path.startswith("/share/"):
            continue        # a share's token is its key (test_shares.py)
        elif isinstance(route, (APIRoute, Route)) and path not in login_open | {"/"}:
            for method in route.methods - {"HEAD"}:
                r = c.request(method, path)
                assert r.status_code == 404, (method, path, r.status_code)
                assert r.json() == {"error": {"code": "not_found", "message": path}}
                assert "set-cookie" not in r.headers
                checked += 1
        elif isinstance(route, WebSocketRoute):
            with pytest.raises(WebSocketDenialResponse) as e:
                with c.websocket_connect(path):
                    pass
            assert e.value.status_code == 404
            checked += 1
    assert checked > 100
    page = c.get("/")                                 # the login screen is the only page
    assert 'id="celine-login"' in page.text and "/assets/" not in page.text


def test_the_first_passkey_with_the_setup_code(app, caplog):
    with caplog.at_level(logging.WARNING, logger="celine.auth"):
        app.state.auth.announce_setup_code()
    [line] = [r.getMessage() for r in caplog.records if "setup code" in r.getMessage()]
    f = app.state.auth.setup_code_file          # the code is only in this file
    code = f.read_text().strip()
    assert re.fullmatch(r"(?:[A-Z2-7]{4} ){6}[A-Z2-7]{4}", code)
    assert f.name == "setup-code.txt" and code not in line and str(f) in line
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    # a passkey made for another origin or relying party is refused
    for kw in ({"origin": "http://evil.example"}, {"rp_id": "evil.example"}):
        assert register(client(app), code, SoftPasskey(), **kw).status_code == 400
    assert not app.state.auth.store.has_credentials()
    c = client(app)
    r = register(c, code, SoftPasskey())
    assert r.status_code == 200
    app.state.auth.announce_setup_code()        # a passkey exists: the file goes
    assert not f.exists()
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie
    assert c.get("/api/health").json() == {"ok": True}
    # neither the code nor the session is stored, only their hashes, in a private file
    db = Path(os.environ["CELINE_DATA_DIR"]) / "auth.db"
    raw = db.read_bytes()
    assert credentials.normalize(code).encode() not in raw and c.cookies[COOKIE].encode() not in raw
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    # the setup code is used up
    again = client(app).post("/api/auth/register/start", json={"code": code})
    assert again.status_code == 400 and again.json()["error"]["code"] == "wrong_code"


def test_login_replay_and_logout(app):
    pk = SoftPasskey()
    setup_first_passkey(app, client(app), pk)
    c = client(app)
    assert login(c, SoftPasskey()).status_code == 401          # not registered here
    # forwarded headers do not change the relying party
    assert c.post("/api/auth/login/start", headers={
        "x-forwarded-host": "evil.example", "x-forwarded-proto": "https"}
    ).json()["options"]["rpId"] == "localhost"
    start = c.post("/api/auth/login/start").json()
    cred = pk.get(start["options"])
    ok = c.post("/api/auth/login/finish", json={"ceremony": start["ceremony"], "credential": cred})
    assert ok.status_code == 200 and c.get("/api/health").status_code == 200
    again = client(app).post("/api/auth/login/finish",
                             json={"ceremony": start["ceremony"], "credential": cred})
    assert again.status_code == 401                            # the challenge is gone
    r = c.post("/api/auth/logout")
    assert r.headers["clear-site-data"] == '"cache", "cookies"'
    c.cookies.clear()
    assert c.get("/api/health").status_code == 404


def test_sessions_slide_expire_and_end_after_90_days(app):
    clock = app.state.auth.store.clock = Clock()
    pk = SoftPasskey()
    c = client(app)
    setup_first_passkey(app, c, pk)
    clock.t += 31 * 86400                                       # 31 idle days: gone
    assert c.get("/api/health").status_code == 404
    assert login(c, pk).status_code == 200
    for day in range(10, 100, 10):                              # used every 10 days
        clock.t += 10 * 86400
        assert c.get("/api/health").status_code == (200 if day < 90 else 404), day


def test_a_recovery_code_adds_a_new_passkey_once(app):
    rc = setup_first_passkey(app, client(app), SoftPasskey()).json()["recovery_codes"]
    lost = client(app)                                  # a new browser, the passkey is lost
    r = register(lost, rc[3], SoftPasskey())
    assert r.status_code == 200 and "recovery_codes" not in r.json()
    assert len(app.state.auth.store.passkeys()) == 2
    # the same code again: the same answer as a code that never existed
    used = client(app).post("/api/auth/register/start", json={"code": rc[3]})
    never = client(app).post("/api/auth/register/start", json={"code": credentials.new_code()})
    assert used.status_code == 400 and used.json() == never.json()


def test_adding_and_removing_passkeys(app):
    c = client(app)
    first = SoftPasskey()
    setup_first_passkey(app, c, first)
    other = client(app)
    assert login(other, first).status_code == 200
    # the ceremony finishes only in the session that started it
    start = c.post("/api/auth/passkeys/start").json()
    r = other.post("/api/auth/passkeys/finish", json={
        "ceremony": start["ceremony"], "credential": SoftPasskey().create(start["options"])})
    assert r.status_code == 400
    start = c.post("/api/auth/passkeys/start").json()
    second = SoftPasskey()
    assert c.post("/api/auth/passkeys/finish", json={
        "ceremony": start["ceremony"], "credential": second.create(start["options"])}
    ).status_code == 200
    keys = c.get("/api/auth/credentials").json()["credentials"]
    third = client(app)
    assert login(third, second).status_code == 200
    # removing one ends its sessions, not this one
    assert c.delete(f"/api/auth/credentials/{keys[1]['id']}").json() == \
        {"ok": True, "logged_out": False}
    assert third.get("/api/health").status_code == 404 and c.get("/api/health").status_code == 200


def test_wrong_codes_lock_code_entry_everywhere_and_failed_logins_one_address(app, caplog):
    auth = app.state.auth
    clock = auth.store.clock = Clock()
    good = auth.store.new_setup_code()
    c = client(app)
    with caplog.at_level(logging.WARNING, logger="celine.auth"):
        for _ in range(store_mod.FREE_TRIES):
            r = c.post("/api/auth/register/start", json={"code": credentials.new_code()})
    assert r.status_code == 429 and r.json()["error"]["locked_until"] == clock.t + 6 * 3600
    # locked: even the right code is not checked, from any address
    assert c.post("/api/auth/register/start", json={"code": good}).status_code == 429
    other = client(app, address="203.0.113.9")
    assert other.post("/api/auth/register/start", json={"code": good}).status_code == 429
    # every failure and each lock is logged, never with a code
    text = "\n".join(rec.getMessage() + str(rec.__dict__) for rec in caplog.records)
    assert "auth.locked" in text and good not in text and credentials.normalize(good) not in text
    clock.t = r.json()["error"]["locked_until"] + 1
    pk = SoftPasskey()
    assert register(c, good, pk).status_code == 200
    for _ in range(10):
        assert login(client(app), SoftPasskey()).status_code == 401
    assert client(app).post("/api/auth/login/start").status_code == 429
    assert login(client(app, address="203.0.113.9"), pk).status_code == 200


# ---- password and PIN ------------------------------------------------------------------

PASSWORD = "correct horse battery"


def setup_password(app, c: TestClient, **kw):
    code = app.state.auth.store.new_setup_code()
    return c.post("/api/auth/register/credential",
                  json={"code": code, "kind": "password", "secret": PASSWORD, **kw})


def test_a_password_with_the_setup_code_then_logging_in_with_it(app):
    auth = app.state.auth
    auth.announce_setup_code()
    code = auth.setup_code_file.read_text().strip()
    c = client(app)
    short = c.post("/api/auth/register/credential",
                   json={"code": code, "kind": "password", "secret": "short"})
    assert short.status_code == 400
    assert short.json()["error"]["message"] == "A password has at least 10 characters."
    assert auth.store.setup_code() is not None           # a bad secret spends no code
    r = c.post("/api/auth/register/credential",
               json={"code": code, "kind": "password", "secret": PASSWORD, "label": "laptop"})
    assert r.status_code == 200, r.text
    assert len(r.json()["recovery_codes"]) == 10 and "HttpOnly" in r.headers["set-cookie"]
    assert not auth.setup_code_file.exists() and auth.store.has_credentials()
    assert c.get("/api/auth/status").json() == {"login": True, "methods": ["password"],
                                                "setup": False}
    [cred] = c.get("/api/auth/credentials").json()["credentials"]
    assert cred["kind"] == "password" and cred["label"] == "laptop" and cred["this_session"]
    assert PASSWORD.encode() not in (Path(os.environ["CELINE_DATA_DIR"]) / "auth.db").read_bytes()
    # the login page knows the methods; the code is used up
    assert 'const METHODS = ["password"]' in client(app).get("/").text
    assert client(app).post("/api/auth/register/start", json={"code": code}).status_code == 400
    # log in with it, from a new browser
    fresh = client(app)
    wrong = fresh.post("/api/auth/login", json={"method": "password", "secret": "not it, no"})
    assert wrong.status_code == 401 and wrong.json()["error"]["message"] == "The password is wrong."
    assert fresh.post("/api/auth/login", json={"method": "pin", "secret": "12345678"}).status_code == 401
    assert fresh.post("/api/auth/login", json={"method": "x", "secret": PASSWORD}).status_code == 400
    ok = fresh.post("/api/auth/login", json={"method": "password", "secret": PASSWORD})
    assert ok.status_code == 200 and fresh.get("/api/health").json() == {"ok": True}


def test_wrong_passwords_lock_passwords_and_pins_from_that_address_never_the_codes(app):
    auth = app.state.auth
    clock = auth.store.clock = Clock()
    recovery = setup_password(app, client(app)).json()["recovery_codes"]
    c = client(app, address="198.51.100.7")
    for n in range(store_mod.FREE_TRIES):
        r = c.post("/api/auth/login", json={"method": "password", "secret": f"wrong one {n}"})
    assert r.status_code == 429 and r.json()["error"]["locked_until"] == clock.t + 6 * 3600
    assert r.json()["error"]["message"] == \
        "Too many wrong passwords or PINs from here. Logging in with them is locked."
    # locked for that address, the right password too, and PINs with it
    assert c.post("/api/auth/login", json={"method": "password", "secret": PASSWORD}
                  ).status_code == 429
    assert c.post("/api/auth/login", json={"method": "pin", "secret": "12345678"}
                  ).status_code == 429
    # another address is not locked: nobody locks the owner out from elsewhere
    other = client(app, address="203.0.113.9")
    assert other.post("/api/auth/login", json={"method": "password", "secret": PASSWORD}
                      ).status_code == 200
    # a recovery code still works from the locked address
    pin = c.post("/api/auth/register/credential",
                 json={"code": recovery[0], "kind": "pin", "secret": "04815162"})
    assert pin.status_code == 200, pin.text
    # the next wrong one after the lock doubles it
    clock.t = r.json()["error"]["locked_until"] + 1
    again = c.post("/api/auth/login", json={"method": "pin", "secret": "11111111"})
    assert again.status_code == 429 and again.json()["error"]["locked_until"] == clock.t + 12 * 3600
    clock.t += 12 * 3600 + 1
    assert c.post("/api/auth/login", json={"method": "pin", "secret": "04815162"}
                  ).status_code == 200
    # a right one cleared the count: the next wrong one is the first again
    wrong = c.post("/api/auth/login", json={"method": "password", "secret": "wrong again"})
    assert wrong.status_code == 401 and wrong.json()["error"]["message"] == "The password is wrong."
    # failed logins of any kind count per address too: 10 in 15 minutes
    d = client(app, address="192.0.2.44")
    for _ in range(10):
        assert login(d, SoftPasskey()).status_code == 401
    assert d.post("/api/auth/login/start").status_code == 429
    assert d.post("/api/auth/login", json={"method": "password", "secret": PASSWORD}
                  ).json()["error"]["code"] == "rate_limited"


def test_wrong_codes_lock_the_codes_never_the_password(app):
    auth = app.state.auth
    clock = auth.store.clock = Clock()
    recovery = setup_password(app, client(app)).json()["recovery_codes"]
    c = client(app)
    for _ in range(store_mod.FREE_TRIES):
        r = c.post("/api/auth/register/start", json={"code": credentials.new_code()})
    assert r.status_code == 429 and r.json()["error"]["locked_until"] == clock.t + 6 * 3600
    assert r.json()["error"]["message"] == "Too many wrong codes. Code entry is locked."
    assert c.post("/api/auth/register/credential", json={
        "code": recovery[0], "kind": "pin", "secret": "04815162"}).status_code == 429
    # the password still works, and its right answer leaves the code lock alone
    assert c.post("/api/auth/login", json={"method": "password", "secret": PASSWORD}
                  ).status_code == 200
    assert c.post("/api/auth/register/start", json={"code": recovery[0]}).status_code == 429
    clock.t = r.json()["error"]["locked_until"] + 1
    assert c.post("/api/auth/register/start", json={"code": recovery[0]}).status_code == 200


def test_a_pin_is_exactly_eight_digits(app):
    c = client(app)
    code = app.state.auth.store.new_setup_code()
    for bad in ("1234", "123456789", "abcdefgh", "1234 5678"):
        r = c.post("/api/auth/register/credential", json={"code": code, "kind": "pin", "secret": bad})
        assert r.status_code == 400 and r.json()["error"]["message"] == "A PIN is exactly 8 digits."
    r = c.post("/api/auth/register/credential", json={"code": code, "kind": "token", "secret": "x" * 12})
    assert r.status_code == 400 and r.json()["error"]["message"] == "The kind is password or pin."
    assert c.post("/api/auth/register/credential",
                  json={"code": code, "kind": "pin", "secret": "04815162"}).status_code == 200
    assert c.post("/api/auth/credentials", json={"kind": "pin", "secret": "0481516"}).status_code == 400
    fresh = client(app)
    assert fresh.post("/api/auth/login", json={"method": "pin", "secret": "04815162"}).status_code == 200
    assert fresh.get("/api/auth/status").json()["methods"] == ["pin"]


def test_the_last_way_to_log_in_stays(app):
    c = client(app)
    assert setup_password(app, c).status_code == 200
    [pw] = c.get("/api/auth/credentials").json()["credentials"]
    r = c.delete(f"/api/auth/credentials/{pw['id']}")
    assert r.status_code == 409 and r.json()["error"]["message"] == "This is your last way to log in."
    assert c.delete("/api/auth/credentials/999").status_code == 404
    added = c.post("/api/auth/credentials", json={"kind": "pin", "secret": "11112222", "label": "phone"})
    assert added.status_code == 200
    pk = SoftPasskey()
    start = c.post("/api/auth/passkeys/start").json()
    assert c.post("/api/auth/passkeys/finish", json={
        "ceremony": start["ceremony"], "credential": pk.create(start["options"])}).status_code == 200
    rows = c.get("/api/auth/credentials").json()["credentials"]
    assert [r["kind"] for r in rows] == ["passkey", "password", "pin"]
    assert [r["this_session"] for r in rows] == [False, True, False]
    assert c.get("/api/auth/status").json()["methods"] == ["passkey", "password", "pin"]
    # removing the password ends its sessions, this one among them
    assert c.delete(f"/api/auth/credentials/{pw['id']}").json() == {"ok": True, "logged_out": True}
    assert c.get("/api/health").status_code == 404
    page = client(app).get("/").text
    assert 'const METHODS = ["passkey", "pin"]' in page and "const SETUP = false" in page
    other = client(app)
    assert login(other, pk).status_code == 200
    assert other.delete(f"/api/auth/credentials/{rows[0]['id']}").json() == {"ok": True, "logged_out": True}


def test_the_login_page_uses_the_app_tokens_value_for_value():
    from celine.auth.page import _PAGE

    def decls(css: str) -> dict[str, str]:
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        out: dict[str, str] = {}
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;}]+)", css):
            out.setdefault(name, " ".join(value.split()))
        return out
    tokens = decls((Path(__file__).parents[1] / "web/src/styles/tokens.css").read_text())
    page = decls(re.search(r":root\{(.*?)\}", _PAGE, re.S)[1])
    assert page and {k: v for k, v in page.items() if tokens.get(k) != v} == {}
    used = set(re.findall(r"var\((--[\w-]+)\)", _PAGE))
    assert used <= set(page), used - set(page)


def test_answers_cannot_be_framed_and_https_cookies_are_secure(app):
    for r in (client(app).get("/"), client(app).get("/api/sources")):
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
    https = make_app(public_origin="https://celine.example.net")
    pk = SoftPasskey("celine.example.net", "https://celine.example.net")
    r = setup_first_passkey(https, client(https, base="https://celine.example.net"), pk)
    assert "Secure" in r.headers["set-cookie"]


def test_login_off_is_for_loopback_only():
    assert startup_problem({"login": "off"}, "::1", 8766, False) is None
    for host in ("0.0.0.0", "::", "10.0.0.5"):
        assert startup_problem({"login": "off"}, host, 8766, False)


def test_a_trusted_proxy_is_one_private_address_or_network():
    on = {"login": "on"}
    for ok in ([], ["192.168.1.2"], ["172.18.0.0/16", "10.0.0.5"], ["127.0.0.1"], ["fd00::/8"]):
        assert startup_problem({**on, "trusted_proxies": ok}, "0.0.0.0", 8766, False) is None
    for bad in (["*"], ["0.0.0.0/0"], ["::/0"], ["8.8.8.0/24"], ["10.0.0.5", "8.8.4.4"],
                ["proxy.example"]):
        assert "CELINE_TRUSTED_PROXIES" in startup_problem(
            {**on, "trusted_proxies": bad}, "0.0.0.0", 8766, False)


def test_a_missing_config_is_a_one_line_refusal(tmp_path):
    env = {**os.environ, "CELINE_CONFIG_DIR": str(tmp_path)}
    for args in ([], ["doctor"]):
        r = subprocess.run([sys.executable, "-m", "celine", *args], env=env,
                           capture_output=True, text=True, timeout=60,
                           cwd=Path(__file__).resolve().parents[1])
        assert r.returncode == 2 and r.stderr.startswith("celine: no config/celine.toml")


def test_wrong_pins_from_many_addresses_pause_pin_login_for_everyone_not_the_password(app):
    auth = app.state.auth
    clock = auth.store.clock = Clock()
    recovery = setup_password(app, client(app)).json()["recovery_codes"]
    assert client(app).post("/api/auth/register/credential", json={
        "code": recovery[0], "kind": "pin", "secret": "04815162"}).status_code == 200
    for n in range(store_mod.PIN_FAILS):                        # a new address every time
        r = client(app, address=f"2001:db8::{n + 1:x}").post(
            "/api/auth/login", json={"method": "pin", "secret": "11111111"})
    assert r.status_code == 401
    other = client(app, address="2001:db8:1::1")
    paused = other.post("/api/auth/login", json={"method": "pin", "secret": "04815162"})
    assert paused.status_code == 429
    assert paused.json()["error"]["locked_until"] == clock.t + store_mod.PIN_PAUSE
    assert other.post("/api/auth/login", json={"method": "password", "secret": PASSWORD}
                      ).status_code == 200
    clock.t += store_mod.PIN_PAUSE + 1
    assert client(app, address="2001:db8:2::1").post(
        "/api/auth/login", json={"method": "pin", "secret": "04815162"}).status_code == 200
