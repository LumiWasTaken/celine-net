"""The login: a passkey (WebAuthn, via py_webauthn), a password or an 8-digit PIN.

`[server] login` (env CELINE_LOGIN) is "off" or "on". Off is for local runs and is
allowed only while the server binds to loopback; the Docker image turns it on.

With login on, a request without a valid session sees nothing: every path answers
the same 404 as an unknown route, the WebSocket too (gate.py). The exceptions are the
login screen at `/` and the endpoints it calls (routes.py, open_router). The relying party
(RP ID and origin) comes from `[server] public_origin` (CELINE_PUBLIC_ORIGIN), never
from request headers; the default is http://localhost:<port>.

Why py_webauthn: the Duo Labs library is the maintained, spec-tracking WebAuthn
server for Python (attestation and assertion checks, sign counts), on `cryptography`.
"""
from __future__ import annotations

import ipaddress
import logging
import secrets
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .store import AuthStore

log = logging.getLogger("celine.auth")

COOKIE = "celine_session"
MODES = ("off", "on")

CEREMONY_SECONDS = 300          # a started login or registration must finish in 5 min
MAX_CEREMONIES = 1024           # started and not finished, all clients together
CLIENT_CEREMONIES = 4           # per client address, so one address cannot push out
                                # the user's login in progress
LOGIN_MAX_FAILURES = 10         # failed logins per client address ...
LOGIN_WINDOW = 15 * 60          # ... within 15 minutes, then refused for the rest of it


class AuthConfigError(Exception):
    pass


def mode(server: dict) -> str:
    m = str(server.get("login", "off") or "off").strip().lower()
    if m not in MODES:
        raise AuthConfigError(f'[server] login = "{m}" is not a mode. Use "off" or "on".')
    return m


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host in ("localhost", "localhost.localdomain")


def startup_problem(server: dict, host: str, port: int, dev: bool) -> str | None:
    """One sentence when the server must not start: the login off on anything but
    loopback (so a forgotten setting can never open the live server), or a login
    setting that does not work, or a trusted proxy that would let anyone pick their
    own client address."""
    if (bad := _proxy_problem(server.get("trusted_proxies"))):
        return bad
    try:
        if mode(server) == "on":
            settings(server, port, dev)
            return None
    except AuthConfigError as e:
        return str(e)
    if not is_loopback(host):
        return (f"The login is off, so Celine may only bind to loopback (127.0.0.1 or "
                f"::1), not {host}. Set CELINE_LOGIN=on (docs/DEPLOY.md).")
    return None


def _proxy_problem(proxies) -> str | None:
    """A trusted proxy must be one address or network on loopback or a private range:
    a wider one makes the forwarded client address (the login limit's key) the
    caller's choice."""
    proxies = [proxies] if isinstance(proxies, str) else list(proxies or [])
    for p in (str(x).strip() for x in proxies):
        try:
            net = ipaddress.ip_network(p, strict=False)
        except ValueError:
            net = None
        if net is None or net.prefixlen == 0 or not (net.is_private or net.is_loopback):
            return (f"CELINE_TRUSTED_PROXIES has {p!r}: only the reverse proxy's own "
                    f"address (such as 172.18.0.2), never *, a public range or "
                    f"everything (docs/DEPLOY.md).")
    return None


@dataclass
class Settings:
    rp_id: str
    rp_name: str
    user_name: str
    origin: str
    origins: list[str]
    secure: bool


def settings(server: dict, port: int, dev: bool, rp_name: str = "Celine",
             user_name: str = "owner") -> Settings:
    """The relying party from the deployment config. Raises AuthConfigError."""
    origin = str(server.get("public_origin") or f"http://localhost:{port}").rstrip("/")
    parts = urlsplit(origin)
    host = parts.hostname or ""
    if (parts.scheme not in ("http", "https") or not host or parts.path or parts.query
            or parts.fragment):
        raise AuthConfigError(f"The public origin {origin!r} must look like "
                              f"https://celine.example.net or http://localhost:{port}.")
    try:
        ipaddress.ip_address(host)
        is_address = True
    except ValueError:
        is_address = False
    if is_address:
        raise AuthConfigError(f"Passkeys need a host name, not the address {host}. "
                              f"Use http://localhost:{port} locally.")
    if parts.scheme == "http" and host != "localhost":
        raise AuthConfigError(f"Passkeys need https for {host}. Only localhost may use "
                              f"http.")
    origins = [origin]
    if dev and host == "localhost":
        origins += [f"http://localhost:{p}" for p in (5174, 4173)]   # vite dev, vite preview
    return Settings(rp_id=host, rp_name=rp_name, user_name=user_name, origin=origin,
                    origins=origins, secure=parts.scheme == "https")


@dataclass
class Ceremony:
    kind: str                       # "login", "register" (with a code) or "add"
    challenge: bytes
    expires: float
    client: str
    code: tuple[str, Any] | None = None   # ("setup", its hash) or ("recovery", row id)
    session: bytes | None = None          # adding a passkey: the session's token hash


@dataclass
class Auth:
    settings: Settings
    store: AuthStore
    ceremonies: OrderedDict[str, Ceremony] = field(default_factory=OrderedDict)
    login_failures: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(deque))

    @classmethod
    def open(cls, data_dir: Path, s: Settings) -> "Auth":
        return cls(settings=s, store=AuthStore(data_dir / "auth.db"))

    # ---- ceremonies (one browser's started login or registration) --------------------
    def begin(self, c: Ceremony) -> str:
        now = time.monotonic()
        for k in [k for k, v in self.ceremonies.items() if v.expires < now]:
            del self.ceremonies[k]
        mine = [k for k, v in self.ceremonies.items() if v.client == c.client]
        for k in mine[:max(0, len(mine) - CLIENT_CEREMONIES + 1)]:
            del self.ceremonies[k]
        while len(self.ceremonies) >= MAX_CEREMONIES:
            self.ceremonies.popitem(last=False)
        cid = secrets.token_urlsafe(18)
        self.ceremonies[cid] = c
        return cid

    def take(self, cid: str, kind: str) -> Ceremony | None:
        c = self.ceremonies.pop(cid, None) if isinstance(cid, str) else None
        if c is None or c.kind != kind or c.expires < time.monotonic():
            return None
        return c

    # ---- failed logins, per client address -----------------------------------
    def login_limited(self, client: str) -> bool:
        now = time.monotonic()
        q = self.login_failures.get(client)
        while q and now - q[0] > LOGIN_WINDOW:
            q.popleft()
        if q is not None and not q:
            del self.login_failures[client]
        return q is not None and len(q) >= LOGIN_MAX_FAILURES

    def login_failed(self, client: str) -> None:
        now = time.monotonic()
        self.login_failures[client].append(now)
        if len(self.login_failures) > 10_000:      # forget addresses whose window ended
            for k in [k for k, q in self.login_failures.items() if now - q[-1] > LOGIN_WINDOW]:
                del self.login_failures[k]

    # ---- the setup code ---------------------------------------------------------------
    @property
    def setup_code_file(self) -> Path:
        """<data_dir>/setup-code.txt: where the setup code is (never the log)."""
        return self.store.path.with_name("setup-code.txt")

    def announce_setup_code(self) -> None:
        """No way to log in yet: a fresh one-time code goes to
        setup-code.txt in the data dir (mode 600). Each start makes a new one; the one
        before stops working. With a passkey, password or PIN the file goes."""
        if self.store.has_credentials():
            self.forget_setup_code()
            return
        code = self.store.new_setup_code()
        try:
            self.setup_code_file.write_text(code + "\n")
            self.setup_code_file.chmod(0o600)
        except OSError as e:
            log.warning("setup code not written to %s: %s", self.setup_code_file, e.strerror,
                        extra={"event": "auth.setup_code_file_failed"})
        log.warning(f"No login is set up yet. The setup code for the first passkey, "
                    f"password or PIN is in {self.setup_code_file}; open "
                    f"{self.settings.origin} and enter it.",
                    extra={"event": "auth.setup_code"})

    def forget_setup_code(self) -> None:
        """The file goes once a login exists (also to call right after the first
        registration, auth/routes.py)."""
        self.setup_code_file.unlink(missing_ok=True)

    def cookie(self, token: str, max_age: int) -> dict:
        return {"key": COOKIE, "value": token, "max_age": max_age, "path": "/",
                "httponly": True, "samesite": "strict", "secure": self.settings.secure}
