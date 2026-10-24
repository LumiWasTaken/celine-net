"""The login endpoints: passkey, password and PIN.

`open_router` holds the routes reached without a session (the gate builds its list
from it): logging in, and adding the first way to log in with the setup code or a
new one with a recovery code. `router` holds the rest, reached only with a session
(Settings, Security): the ways to log in and the proof.

The proof: adding a way to log in, new recovery codes and a named token need the owner
to have proved it in this session within PROOF_SECONDS (api/deps.py `proven`, else 403
`proof_needed`). A login is one; /reauth (a password or PIN) and /reauth/start and
/reauth/finish (a passkey) give another, under the same limits as logging in.

Two limits guard the open routes. Per client address, LOGIN_MAX_FAILURES failed
logins (passkey, password or PIN) refuse that address's logins for the rest of
LOGIN_WINDOW (the `not_limited` dependency). And the two locks of store.py: wrong
setup or recovery codes lock code entry, wrong passwords or PINs lock those, each
kind on its own.

Failed attempts are logged with the client address and the kind of failure, never a
code, a challenge or a credential.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import anyio
import webauthn
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json_dict
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor, ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..api.deps import proven
from ..errors import CelineError
from . import CEREMONY_SECONDS, Auth, Ceremony, credentials as cred
from .store import SESSION_IDLE, Session

log = logging.getLogger("celine.auth")
open_router = APIRouter(prefix="/api/auth")
router = APIRouter(prefix="/api/auth")

# how long the browser's passkey prompt waits; the server keeps the ceremony for
# CEREMONY_SECONDS, longer, so an answer given at the prompt's last moment still lands
TIMEOUT_MS = 120_000


def _auth(request: Request) -> Auth:
    return request.app.state.auth


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def not_limited(request: Request) -> None:
    """The per-address limit of the three login routes."""
    if _auth(request).login_limited(_client(request)):
        raise CelineError("rate_limited", "Too many failed logins. Try again in 15 minutes.",
                          429)


def _err(code: str, message: str, status: int, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}},
                        status_code=status)


def _reject(request: Request, kind: str, reason: str, answer: JSONResponse, *,
            login: bool = False, **extra: Any) -> JSONResponse:
    """Logs a failed attempt and returns its answer. `login`: a failed login, which
    also counts toward the address's limit."""
    if login:
        _auth(request).login_failed(_client(request))
    log.warning("login attempt failed", extra={"event": "auth.failed", "kind": kind,
                                              "client": _client(request), "reason": reason,
                                              **extra})
    return answer


def _signed_in(request: Request, body: dict, passkey: bytes | None = None,
               credential: int | None = None) -> JSONResponse:
    auth = _auth(request)
    res = JSONResponse(body)
    token = auth.store.new_session(passkey, credential, request.headers.get("user-agent", ""))
    res.set_cookie(**auth.cookie(token, SESSION_IDLE))
    return res


def _session(request: Request) -> Session | None:
    return getattr(request.state, "session", None)


class Finish(BaseModel):
    ceremony: str
    credential: dict[str, Any]


class CodeIn(BaseModel):
    code: str


class LoginIn(BaseModel):
    method: str
    secret: str


class CredentialIn(BaseModel):
    kind: str
    secret: str
    label: str = ""


class RegisterCredentialIn(CredentialIn):
    code: str


def _invalid(kind: str, secret: str) -> str | None:
    """One sentence when this is not a password or a PIN."""
    if kind == "pin":
        return None if len(secret) == 8 and secret.isdigit() else "A PIN is exactly 8 digits."
    if kind == "password":
        return None if len(secret) >= 10 else "A password has at least 10 characters."
    return "The kind is password or pin."


# ---- the two locks: one try, checked against stored hashes ------------------------------

_LOCKED = {"code": "Too many wrong codes. Code entry is locked.",
           "secret": "Too many wrong passwords or PINs from here. Logging in with them is locked.",
           "pin": "Too many wrong PINs. PIN login is paused."}


async def _try(request: Request, lock: str, kind: str, typed: str,
               rows: list[tuple[Any, bytes]], at_least: int, wrong: JSONResponse):
    """One try under `lock` ("code" or "secret", store.py): the ref of the row `typed`
    opens, or the answer to refuse with. A wrong password or PIN is a failed login."""
    auth = _auth(request)
    login = lock == "secret"
    addr = _client(request) if login else ""
    if kind == "pin" and (until := auth.store.pin_paused()):
        return _reject(request, kind, "locked", _locked("pin", until))
    until, failures = auth.store.attempt(lock, addr)
    if not failures:
        return _reject(request, kind, "locked", _locked(lock, until))
    ref = await anyio.to_thread.run_sync(cred.first_match, typed, rows, at_least)
    if ref is None:
        if kind == "pin":
            auth.store.pin_failed()
        if until:
            log.warning("locked", extra={
                "event": "auth.locked", "lock": lock, "failures": failures, "until": until,
                "hours": round((until - auth.store.clock()) / 3600, 1)})
            wrong = _locked(lock, until)
        return _reject(request, kind, "wrong", wrong, login=login, failures=failures)
    auth.store.succeeded(lock, addr)
    return ref


def _locked(lock: str, until: int) -> JSONResponse:
    return _err("locked", _LOCKED[lock], 429, locked_until=until)


async def _take_code(request: Request, typed: str):
    """The setup code ("setup", hash) or an unused recovery code ("recovery", id) this
    is, or the answer to refuse with. Always hashes against RECOVERY_COUNT + 1 rows, so
    the time taken says nothing about how many codes are left."""
    store = _auth(request).store
    setup = store.setup_code() if not store.has_credentials() else None
    rows = [(("setup", setup), setup)] if setup else []
    rows += [(("recovery", rid), h) for rid, h in store.unused_recovery_codes()]
    return await _try(request, "code", "code", cred.normalize(typed), rows,
                      cred.RECOVERY_COUNT + 1,
                      _err("wrong_code", "That code does not work.", 400))


def _claim(auth: Auth, ref: tuple[str, Any]) -> bool:
    kind, key = ref
    return auth.store.claim_setup_code(key) if kind == "setup" else auth.store.use_recovery_code(key)


async def _after_code(auth: Auth, ref: tuple[str, Any]) -> dict:
    """After the setup code the file goes and the recovery codes come."""
    if ref[0] != "setup":
        return {"ok": True}
    auth.forget_setup_code()
    return {"ok": True,
            "recovery_codes": await anyio.to_thread.run_sync(auth.store.new_recovery_codes)}


# ---- passkey ceremonies: start, then finish ---------------------------------------------

def _start(request: Request, kind: str, challenge: bytes, options: dict, **c: Any) -> dict:
    cid = _auth(request).begin(Ceremony(kind, challenge, time.monotonic() + CEREMONY_SECONDS,
                                        _client(request), **c))
    return {"ceremony": cid, "options": options}


def _registration_options(auth: Auth) -> tuple[bytes, dict]:
    s = auth.settings
    opts = webauthn.generate_registration_options(
        rp_id=s.rp_id, rp_name=s.rp_name, user_name=s.user_name,
        user_display_name=s.user_name, user_id=auth.store.user_handle(),
        timeout=TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=p["id"])
                             for p in auth.store.passkeys()])
    return opts.challenge, options_to_json_dict(opts)


_SETUP_FAILED = ("register_failed", "The passkey setup failed. Start again.", 400)


def _new_passkey(request: Request, kind: str, c: Ceremony, credential: dict):
    """Verifies a registration and stores the passkey: its id, or the refusal."""
    auth = _auth(request)
    try:
        v = webauthn.verify_registration_response(
            credential=credential, expected_challenge=c.challenge,
            expected_rp_id=auth.settings.rp_id, expected_origin=auth.settings.origins,
            require_user_verification=True)
    except Exception as e:                          # py_webauthn raises several kinds
        return _reject(request, kind, "verification failed", _err(
            "register_failed", "The app could not add the passkey. Start again.", 400),
            error=type(e).__name__)
    if c.code is not None and not _claim(auth, c.code):
        return _reject(request, kind, "code already used", _err(*_SETUP_FAILED))
    auth.store.add_passkey(v.credential_id, v.credential_public_key, v.sign_count)
    log.info("passkey added", extra={"event": "auth.passkey_added",
                                     "with": c.code[0] if c.code else "session",
                                     "client": _client(request)})
    return v.credential_id


# ---- login ---------------------------------------------------------------------------

def _authentication_options(request: Request) -> tuple[bytes, dict]:
    opts = webauthn.generate_authentication_options(
        rp_id=_auth(request).settings.rp_id, timeout=TIMEOUT_MS,
        user_verification=UserVerificationRequirement.REQUIRED)
    return opts.challenge, options_to_json_dict(opts)


def _assertion(request: Request, kind: str, body: Finish, session: bytes | None = None):
    """A passkey's answer to a started login ("login") or proof ("reauth", only in the
    session that started it): the passkey's id, or the refusal (a failed login)."""
    auth = _auth(request)
    fail = _err("login_failed", "The login with this passkey failed.", 401)
    c = auth.take(body.ceremony, kind)
    if c is None or c.session != session:
        return _reject(request, "passkey", f"no such {kind} in progress", fail, login=True)
    try:
        raw_id = base64url_to_bytes(str(body.credential.get("rawId") or body.credential["id"]))
    except (KeyError, ValueError, TypeError):
        return _reject(request, "passkey", "malformed credential", fail, login=True)
    known = auth.store.credential(raw_id)
    if known is None:
        return _reject(request, "passkey", "unknown passkey", fail, login=True)
    try:
        v = webauthn.verify_authentication_response(
            credential=body.credential, expected_challenge=c.challenge,
            expected_rp_id=auth.settings.rp_id, expected_origin=auth.settings.origins,
            credential_public_key=known[0], credential_current_sign_count=known[1],
            require_user_verification=True)
    except Exception as e:                          # py_webauthn raises several kinds
        return _reject(request, "passkey", "verification failed", fail, login=True,
                       error=type(e).__name__)
    auth.store.used_passkey(raw_id, v.new_sign_count)
    return raw_id


async def _secret(request: Request, body: LoginIn):
    """A password or a PIN under the password and PIN lock: its row id, or the refusal."""
    if body.method not in ("password", "pin"):
        return _err("bad_request", "The method is password or pin.", 400)
    name = "PIN" if body.method == "pin" else "password"
    return await _try(request, "secret", body.method, body.secret,
                      _auth(request).store.credential_hashes(body.method), 1,
                      _err("login_failed", f"The {name} is wrong.", 401))


@open_router.post("/login/start", dependencies=[Depends(not_limited)])
async def login_start(request: Request):
    return _start(request, "login", *_authentication_options(request))


@open_router.post("/login/finish", dependencies=[Depends(not_limited)])
async def login_finish(request: Request, body: Finish):
    raw_id = _assertion(request, "login", body)
    if isinstance(raw_id, JSONResponse):
        return raw_id
    log.info("logged in", extra={"event": "auth.login", "client": _client(request)})
    return _signed_in(request, {"ok": True}, passkey=raw_id)


@open_router.post("/login", dependencies=[Depends(not_limited)])
async def login_secret(request: Request, body: LoginIn):
    """A password or a PIN: the per-address limit and the password and PIN lock."""
    rid = await _secret(request, body)
    if isinstance(rid, JSONResponse):
        return rid
    log.info("logged in", extra={"event": "auth.login", "client": _client(request),
                                 "with": body.method})
    return _signed_in(request, {"ok": True}, credential=rid)


# ---- registration with the setup code or a recovery code ------------------------------

@open_router.post("/register/start")
async def register_start(request: Request, body: CodeIn):
    ref = await _take_code(request, body.code)
    if isinstance(ref, JSONResponse):
        return ref
    return _start(request, "register", *_registration_options(_auth(request)), code=ref)


@open_router.post("/register/finish")
async def register_finish(request: Request, body: Finish):
    auth = _auth(request)
    c = auth.take(body.ceremony, "register")
    if c is None or c.code is None:
        return _reject(request, "register", "no such registration in progress",
                       _err(*_SETUP_FAILED))
    pid = _new_passkey(request, "register", c, body.credential)
    if isinstance(pid, JSONResponse):
        return pid
    return _signed_in(request, await _after_code(auth, c.code), passkey=pid)


@open_router.post("/register/credential")
async def register_credential(request: Request, body: RegisterCredentialIn):
    """A password or PIN with the setup code (first start) or a recovery code."""
    auth = _auth(request)
    if msg := _invalid(body.kind, body.secret):
        return _err("bad_request", msg, 400)
    ref = await _take_code(request, body.code)
    if isinstance(ref, JSONResponse):
        return ref
    if not _claim(auth, ref):
        return _reject(request, "register", "code already used",
                       _err("wrong_code", "That code does not work.", 400))
    rid = await anyio.to_thread.run_sync(auth.store.add_credential, body.kind, body.secret,
                                         body.label)
    log.info("credential added", extra={"event": "auth.credential_added", "kind": body.kind,
                                        "with": ref[0], "client": _client(request)})
    return _signed_in(request, await _after_code(auth, ref), credential=rid)


# ---- Settings, Security (the gate lets these through only with a session) -------------

@router.get("/status")
async def status(request: Request) -> dict:
    store = _auth(request).store
    return {"login": True, "methods": store.methods(), "setup": not store.has_credentials()}


@router.get("/credentials")
async def credentials(request: Request) -> dict:
    """Every way to log in, passkeys included (id base64url); this_session marks the
    one this session came from."""
    return {"credentials": _all_credentials(_auth(request), _session(request))}


def _all_credentials(auth: Auth, s) -> list[dict]:
    out = [{"id": bytes_to_base64url(p["id"]), "kind": "passkey", "label": "",
            "created_at": p["created_at"], "last_used_at": p["last_used_at"],
            "this_session": s is not None and p["id"] == s.passkey_id}
           for p in auth.store.passkeys()]
    out += [{**c, "id": str(c["id"]), "last_used_at": None,
             "this_session": s is not None and c["id"] == s.credential_id}
            for c in auth.store.credentials()]
    return out


@router.post("/credentials", dependencies=[Depends(proven)])
async def add_credential(request: Request, body: CredentialIn):
    auth = _auth(request)
    if msg := _invalid(body.kind, body.secret):
        return _err("bad_request", msg, 400)
    rid = await anyio.to_thread.run_sync(auth.store.add_credential, body.kind, body.secret,
                                         body.label)
    log.info("credential added", extra={"event": "auth.credential_added", "kind": body.kind,
                                        "with": "session", "client": _client(request)})
    return {"ok": True, "id": str(rid)}


@router.delete("/credentials/{cid}")
async def remove_credential(request: Request, cid: str):
    """A passkey id (base64url) or a credential row id. Never the last one."""
    auth = _auth(request)
    s = _session(request)
    mine = _all_credentials(auth, s)
    this = next((c for c in mine if c["id"] == cid), None)
    if this is None:
        return _err("not_found", "That login method is not set up.", 404)
    if len(mine) == 1:
        return _err("last_credential", "This is your last way to log in.", 409)
    if this["kind"] == "passkey":
        auth.store.remove_passkey(base64url_to_bytes(cid))
    else:
        auth.store.remove_credential(int(cid))
    log.info("credential removed", extra={"event": "auth.credential_removed",
                                          "kind": this["kind"], "left": len(mine) - 1})
    return {"ok": True, "logged_out": this["this_session"]}


@router.post("/passkeys/start", dependencies=[Depends(proven)])
async def add_start(request: Request):
    """Needs a fresh proof; /passkeys/finish takes only a ceremony started here, in this
    session, so it needs none of its own."""
    return _start(request, "add", *_registration_options(_auth(request)),
                  session=_session(request).token_hash)


@router.post("/passkeys/finish")
async def add_finish(request: Request, body: Finish):
    c = _auth(request).take(body.ceremony, "add")
    if c is None or c.session != _session(request).token_hash:
        return _err(*_SETUP_FAILED)
    pid = _new_passkey(request, "add", c, body.credential)
    return pid if isinstance(pid, JSONResponse) else {"ok": True}


@router.post("/recovery-codes", dependencies=[Depends(proven)])
async def new_codes(request: Request) -> dict:
    fresh = await anyio.to_thread.run_sync(_auth(request).store.new_recovery_codes)
    log.info("new recovery codes", extra={"event": "auth.recovery_codes"})
    return {"recovery_codes": fresh}


# ---- the proof: the owner again, in this session ----------------------------------------

@router.post("/reauth/start", dependencies=[Depends(not_limited)])
async def reauth_start(request: Request):
    return _start(request, "reauth", *_authentication_options(request),
                  session=_session(request).token_hash)


@router.post("/reauth/finish", dependencies=[Depends(not_limited)])
async def reauth_finish(request: Request, body: Finish):
    s = _session(request)
    raw_id = _assertion(request, "reauth", body, session=s.token_hash)
    if isinstance(raw_id, JSONResponse):
        return raw_id
    _auth(request).store.proved(s)
    log.info("proved", extra={"event": "auth.proved", "client": _client(request)})
    return {"ok": True}


@router.post("/reauth", dependencies=[Depends(not_limited)])
async def reauth_secret(request: Request, body: LoginIn):
    rid = await _secret(request, body)
    if isinstance(rid, JSONResponse):
        return rid
    _auth(request).store.proved(_session(request))
    log.info("proved", extra={"event": "auth.proved", "client": _client(request),
                              "with": body.method})
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request):
    auth = _auth(request)
    auth.store.end_session(_session(request))
    res = JSONResponse({"ok": True}, headers={"clear-site-data": '"cache", "cookies"'})
    res.set_cookie(**auth.cookie("", 0))
    return res
