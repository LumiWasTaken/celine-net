"""The agent's vault: its passwords on the user's Vaultwarden, through the Bitwarden CLI `bw` as
a subprocess (the app image carries it, Dockerfile). The agent's tools are vault/skill.py, the
Settings form api/settings.py, Vault.

bw logs in with the API key (settings/vault.py). The master password is never stored:
the user types it in Settings, Vault, unlock() hands it to `bw unlock` once and keeps
only the session key, in this process. A restart, a settings save or lock() leaves the
vault locked until the user types it again; nothing here unlocks on its own. bw's own
files are in var/vault/. bw reads a local copy of the vault, so a sync runs before a
read when the last one is older than SYNC_EVERY seconds. A command that finds the
session gone leaves the vault locked; the status line says so. Secrets go to bw by
environment or stdin, never on its command line.

An entry is typed only on its own sites: origins() reads them from the entry's URIs,
and the browser refuses every other page (browser/browserd.py, secret steps).

Every password the vault holds, and every old one, is in the redaction set
(errors.set_secrets) from the unlock on, and each new one from the moment it is made.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import string
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import anyio

from .. import errors
from ..settings.vault import account
from ..core.event_types import SETTINGS_CHANGED

log = logging.getLogger("celine.vault")

SYNC_EVERY = 300        # seconds a local copy is trusted
TIMEOUT = 60            # seconds for one bw command
LENGTH = 24             # characters of a password it gets
SYMBOLS = "!#%+-=?@_"   # safe in a shell, a URL form and JSON
ALPHABET = string.ascii_letters + string.digits + SYMBOLS

# the status line (Settings, Vault); each one also ends its tool's refusal
NOT_SET = "Not set up"
UNLOCKED = "Unlocked"
LOCKED = "Locked: type the master password to unlock it"
REFUSED_MASTER = "The vault refuses the master password"
NO_COLLECTION = ("The collection in Settings, Vault is not one the agent's account is in: invite and "
                 "confirm that account in the organization and give it that collection")
NO_COLLECTION_SET = ("No collection in Settings, Vault: the agent's entries live only in a "
                     "collection the user shares with her")
TWO_COLLECTIONS = ("Two of the agent's collections have the name in Settings, Vault: give its id "
                   "instead")
REFUSED_KEY = "The vault refuses the client id or the client secret"
UNREACHABLE = "The vault server is not reachable"
NO_BW = "The app has no bw program: install the Bitwarden CLI where the app runs (the Docker image has it)"
BROKEN = "The vault could not start"
ERROR = "The vault gave an error"        # bw said something else: the log has it
# what bw says when it cannot reach the server
_OFFLINE = ("econnrefused", "enotfound", "eai_again", "etimedout", "econnreset",
            "ehostunreach", "enetunreach", "fetch failed", "fetcherror", "socket hang up",
            "certificate", "self signed", "self-signed")


class VaultError(Exception):
    """One plain sentence: the status line."""


class EntryError(Exception):
    """What is wrong with the entry it named, whole sentences that name the tool that
    fixes it; the vault itself is fine."""


def missing(name: str) -> str:
    return f"There is no vault entry {name}. vault__set(name) makes one."


def pick(items: list[dict], name: str) -> tuple[dict | None, list[str]]:
    """The entry it means by name, any name an entry has (the user makes them by hand
    too): the one of exactly that name, else one whose name differs only by case, the
    first by name when several do. -> (it or None, the other names that differ from its
    name only by case)."""
    same = sorted((it for it in items if (it.get("name") or "").casefold() == name.casefold()),
                  key=lambda it: it["name"])
    it = next((it for it in same if it["name"] == name), same[0] if same else None)
    return it, sorted({o["name"] for o in same if it and o["name"] != it["name"]})


def origin(url: str) -> str | None:
    """https://host:port of an address, or None: not https, no host, or a user name or
    password in it (https://reddit.com@evil.example is evil.example). An address without
    a scheme is https. The browser checks the page the same way (browserd.origin_of)."""
    url = (url or "").strip()
    if "://" not in url:
        url = "https://" + url
    try:
        u = urlsplit(url)
        port = u.port or 443
    except ValueError:
        return None
    if u.scheme.lower() != "https" or not u.hostname or u.username is not None \
            or u.password is not None or "@" in u.netloc:
        return None
    return f"https://{u.hostname.lower().rstrip('.')}:{port}"


def origins(it: dict) -> list[str]:
    """The sites an entry may be typed on: the origin of each of its URIs, exact (no
    other subdomain, no other port)."""
    uris = (it.get("login") or {}).get("uris") or []
    return sorted({o for u in uris if (o := origin(u.get("uri") or ""))})


class _Gone(VaultError):
    """The session is gone (logged out, locked): log in once more."""


def reason(e: VaultError) -> str:
    """The error as the middle of a sentence: "the vault is locked"."""
    said = {NOT_SET: "the vault is not set up", LOCKED: "the vault is locked"}.get(str(e), str(e))
    return said[:1].lower() + said[1:]


def password() -> str:
    """LENGTH characters with a capital, a small letter, a digit and a symbol."""
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
        if (any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in SYMBOLS for c in pw)):
            return pw


def _sentence(args: tuple[str, ...], out: str) -> VaultError:
    """bw's failure as the status line; what bw said goes to the log only."""
    low = out.lower()
    if "master password" in low:
        return VaultError(REFUSED_MASTER)
    if "client_id" in low or "client_secret" in low or "invalid_client" in low:
        return VaultError(REFUSED_KEY)
    if any(w in low for w in _OFFLINE):
        return VaultError(UNREACHABLE)
    if "not logged in" in low or "locked" in low:
        return _Gone(LOCKED)
    log.warning("bw failed", extra={"event": "vault.bw_failed", "command": " ".join(args[:2]),
                                    "said": " ".join(out.split())[:500]})
    return VaultError(ERROR)


class Vault:
    """The process's one bw session. Every bw command runs under one lock: bw's files
    take one writer."""

    def __init__(self, cfg, emit=None):
        self.cfg, self.emit = cfg, emit
        self.session: str | None = None
        self.problem: str | None = None     # the status line when something went wrong
        self.said: str | None = None        # the status line the pages were last told
        self.synced: float | None = None
        self.known: set[str] = set()       # every password seen, for the redaction set
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()       # the first connect is over (the dispatcher)
        # set by the app: forgets the vault's waiting approvals (celine/approvals.py)
        self.forget = None

    @property
    def status(self) -> str:
        if account(self.cfg) is None:
            return NOT_SET
        return self.problem or (UNLOCKED if self.session else LOCKED)

    # ---- bw ----------------------------------------------------------------------------

    async def _bw(self, *args: str, env: dict | None = None, stdin: str | None = None) -> str:
        """bw's answer (stdout), or the VaultError its failure means."""
        home = Path(self.cfg.data_dir) / "vault"
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        full = {**os.environ, "BITWARDENCLI_APPDATA_DIR": str(home),
                "BW_NOINTERACTION": "true", **(env or {})}
        if self.session:
            full["BW_SESSION"] = self.session
        # in a thread: subprocess.run outlives a bw that exits before it read its stdin
        try:
            done = await anyio.to_thread.run_sync(lambda: subprocess.run(
                ["bw", *args], env=full, input=stdin, capture_output=True, text=True,
                timeout=TIMEOUT))
        except FileNotFoundError:
            raise VaultError(NO_BW) from None
        except subprocess.TimeoutExpired:
            raise VaultError(UNREACHABLE) from None
        if done.returncode != 0:
            raise _sentence(args, f"{done.stderr}\n{done.stdout}")
        return done.stdout

    def _learn(self, items: list[dict]) -> None:
        for it in items:
            self.known.add((it.get("login") or {}).get("password") or "")
            self.known.update(h.get("password") or "" for h in it.get("passwordHistory") or ())
        self.known.discard("")
        errors.set_secrets(sorted(self.known), "vault")

    def _say(self, problem: str | None) -> None:
        """What went wrong, or nothing; a changed status line goes to every open
        Settings page."""
        self.problem = problem
        if self.status != self.said:
            self.said = self.status
            if self.emit is not None:
                self.emit({"type": SETTINGS_CHANGED, "what": ["vault"]})

    async def _login(self) -> None:
        """From the start: the server and the API key. That logs in and decrypts
        nothing: the vault stays locked until unlock()."""
        self.session, self.synced = None, None
        self.on_lock()
        a = account(self.cfg)
        if a is None:
            return
        try:
            await self._bw("logout")
        except VaultError:
            pass                                    # not logged in: nothing to leave
        await self._bw("config", "server", a.url)
        await self._bw("login", "--apikey",
                       env={"BW_CLIENTID": a.client_id, "BW_CLIENTSECRET": a.client_secret})

    async def connect(self) -> str:
        """Log in with what Settings hold now (at startup, after a save); the vault is
        locked after it. -> the status line."""
        async with self.lock:
            try:
                await self._login()
            except VaultError as e:
                self._say(str(e))
            except Exception as e:          # never up into the lifespan: that ends the app
                log.warning("vault login failed", exc_info=True,
                            extra={"event": "vault.failed", "error": type(e).__name__})
                self.session = None
                self._say(BROKEN)
            else:
                self._say(None)
        self.ready.set()
        return self.status

    async def unlock(self, master: str) -> str:
        """The master password the user typed, to `bw unlock` once; only the session
        key stays. Logs in first when the API key login is gone. -> the status line."""
        async with self.lock:
            if account(self.cfg) is None:
                self._say(None)
                return self.status
            try:
                for again in (False, True):
                    try:
                        if again:
                            await self._login()
                        got = await self._bw("unlock", "--passwordenv", "BW_PASSWORD",
                                             "--raw", env={"BW_PASSWORD": master})
                        break
                    except _Gone:
                        if again:
                            raise
                del master
                self.session, self.synced = got.strip(), None
                self.on_lock()
                self._learn(await self._items())
            except VaultError as e:
                self.session = None
                self._say(LOCKED if isinstance(e, _Gone) else str(e))
            else:
                self._say(None)
                log.info("vault unlocked", extra={"event": "vault.unlocked"})
        return self.status

    async def lock_now(self) -> str:
        """The session key gone; bw locks its copy too. -> the status line."""
        async with self.lock:
            if self.session is not None:
                try:
                    await self._bw("lock")
                except VaultError:
                    pass                            # the key is dropped either way
            self.session = None
            self.on_lock()
            self._say(None)
        return self.status

    def on_lock(self) -> None:
        """What forgets a session's approvals when it ends (celine/approvals.py)."""
        if self.forget is not None:
            self.forget()

    async def _run(self, fn):
        """fn() with the live session. VaultError with the status line: locked when
        there is none, or when bw finds it gone (only the user unlocks again)."""
        async with self.lock:
            if account(self.cfg) is None:
                raise VaultError(NOT_SET)
            if self.session is None:
                if self.problem:            # locked because of this: say it, keep it
                    raise VaultError(self.problem)
                self._say(None)
                raise VaultError(LOCKED)
            try:
                out = await fn()
            except _Gone:
                self.session = None
                self.on_lock()
                self._say(None)
                raise VaultError(LOCKED) from None
            except VaultError as e:
                self._say(str(e))
                raise
            self._say(None)
            return out

    async def _fresh(self) -> None:
        if self.synced is None or time.monotonic() - self.synced > SYNC_EVERY:
            await self._bw("sync")
            self.synced = time.monotonic()

    async def _items(self) -> list[dict]:
        await self._fresh()
        col, _ = await self._shared()
        args = ("list", "items", "--collectionid", col)
        return [it for it in json.loads(await self._bw(*args) or "[]")
                if it.get("type") == 1]

    async def _shared(self) -> tuple[str, str]:
        """The shared collection its entries live in (Settings, Vault: its name or its
        id), as (collection id, organization id). There is no other place: without a
        collection the vault is refused, so nothing it keeps is hidden. Looked up each
        time, so a rename or a new setting takes effect at once. With one it sees that
        collection only, and its owners see all of it. VaultError when it is in no
        collection of that name or id, or in two of that name."""
        a = account(self.cfg)
        want = (a.collection or "").strip() if a else ""
        if not want:
            raise VaultError(NO_COLLECTION_SET)
        await self._fresh()                 # a collection made a minute ago is there
        cols = json.loads(await self._bw("list", "collections") or "[]")
        hits = [c for c in cols if c.get("id") == want] or \
               [c for c in cols if (c.get("name") or "").casefold() == want.casefold()]
        if len(hits) != 1 or not hits[0].get("organizationId"):
            raise VaultError(NO_COLLECTION if not hits else TWO_COLLECTIONS)
        return hits[0]["id"], hits[0]["organizationId"]

    @staticmethod
    def _stdin(item: dict) -> str:
        return base64.b64encode(json.dumps(item).encode()).decode()

    # ---- what the skill and the browser ask -------------------------------------------

    async def items(self) -> list[dict]:
        """Every login entry, synced."""
        async def fn():
            items = await self._items()
            self._learn(items)
            return items
        return await self._run(fn)

    async def entry(self, name: str) -> tuple[dict | None, list[str]]:
        """pick() over every entry."""
        return pick(await self.items(), name)

    async def secret(self, name: str, which: str) -> tuple[dict, str]:
        """An entry's password, or its previous one (which: password or previous).
        -> (the entry it is from, the password). VaultError when the vault cannot
        answer, EntryError when the entry has none or has no site to be typed on."""
        it, _ = await self.entry(name)
        if it is None:
            raise EntryError(missing(name))
        name = it["name"]
        if not origins(it):
            raise EntryError(f"{name} has no https site of its own, so it is typed nowhere. "
                             f"Only {self.cfg.user} adds one, in Vaultwarden.")
        if which == "previous":
            old = it.get("passwordHistory") or []
            if not old or not old[0].get("password"):
                raise EntryError(f"{name} has no previous password: vault__rotate(name) has "
                                 f"not changed it yet.")
            return it, old[0]["password"]
        pw = (it.get("login") or {}).get("password")
        if not pw:
            raise EntryError(f"{name} has no password. vault__rotate(name) gives it one.")
        return it, pw

    async def make(self, name: str, username: str, url: str, notes: str) -> dict:
        """A new entry with a new password. -> the entry."""
        async def fn():
            pw = password()
            self._learn([{"login": {"password": pw}}])
            col, org = await self._shared()
            item = {"type": 1, "name": name, "notes": notes or None, "favorite": False,
                    "fields": [], "reprompt": 0, "folderId": None, "organizationId": org,
                    "collectionIds": [col],
                    "login": {"username": username or None, "password": pw, "totp": None,
                              "uris": [{"match": None, "uri": url}] if url else []}}
            return json.loads(await self._bw("create", "item", stdin=self._stdin(item)))
        return await self._run(fn)

    async def rotate(self, entry: dict) -> dict:
        """A new password for the entry; the old one is the first of its own
        passwordHistory. -> the entry."""
        async def fn():
            item = json.loads(await self._bw("get", "item", entry["id"]))
            old, pw = item["login"].get("password"), password()
            self._learn([{"login": {"password": pw}}])
            item["login"]["password"] = pw
            got = json.loads(await self._bw("edit", "item", item["id"],
                                            stdin=self._stdin(item)))
            history = got.get("passwordHistory") or []
            if old and not (history and history[0].get("password") == old):
                # a bw that kept no history of the change: the old one goes in by hand
                got["passwordHistory"] = [{"lastUsedDate": None, "password": old}, *history]
                got = json.loads(await self._bw("edit", "item", item["id"],
                                                stdin=self._stdin(got)))
            return got
        return await self._run(fn)


async def probe(cfg, services) -> dict[str, str]:
    """Settings, Features: a Vaultwarden is saved (Settings, Vault) and bw is installed."""
    import shutil
    a = account(cfg)
    if a is None:
        return {"state": "unset", "detail": "Fill in the Vaultwarden details below."}
    if shutil.which("bw") is None:
        return {"state": "missing", "detail": "The bw program is not installed where Celine "
                                              "runs. Install the Bitwarden CLI."}
    return {"state": "ready", "detail": f"Connected to {urlsplit(a.url).netloc}."}
