"""var/auth.db: passkeys, credentials (password, PIN), sessions, recovery codes, the setup
code and the two locks.

Its own SQLite file (mode 600) with its own migrations (auth/migrations, tracked in
the file's user_version), apart from the chat data. One connection, guarded by a lock:
the calls are short, and the slow scrypt checks happen outside it (credentials.py).

The locks: wrong tries count per kind, "code" for setup and recovery codes and
"secret" for passwords and PINs. FREE_TRIES wrong ones lock that kind for 6 hours,
each further one doubles the lock up to 30 days, and a right one clears the count.
While a kind is locked even its right secret is refused. "code" is one lock for every
address; "secret" counts per client address, so nobody can lock the owner's password
or PIN out from elsewhere. The other kind is not touched, so a password typo never locks the recovery codes and a
wrong code never locks the password.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..db.migrate import migrate
from . import credentials as cred

MIGRATIONS = Path(__file__).parent / "migrations"

SESSION_IDLE = 30 * 86400        # a session ends 30 days after it was last used
SESSION_MAX = 90 * 86400         # and 90 days after the login, whatever happens
PROOF_SECONDS = 300              # a proof (a login, or /api/auth/reauth) opens what needs
                                 # one for 5 minutes in its session
SESSION_REFRESH = 3600           # the sliding expiry moves at most once an hour
USER_AGENT_MAX = 200             # characters of a browser's user agent kept with its session

FREE_TRIES = 5                   # wrong tries of one kind before its lock
FIRST_LOCK = 6 * 3600            # the first lock: 6 hours
MAX_LOCK = 30 * 86400            # each further failure doubles it, up to 30 days
LOCKS = ("code", "secret")
PIN_FAILS = 20                   # wrong PINs from any address within PIN_WINDOW pause PIN login for all
PIN_WINDOW = 3600
PIN_PAUSE = 3600


def _now() -> int:
    return int(time.time())


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass
class Session:
    id: int
    token_hash: bytes
    passkey_id: bytes | None        # one of the two: what the login used
    credential_id: int | None
    user_agent: str
    created_at: int
    refreshed_at: int               # also when it was last seen (to the hour)
    expires_at: int
    proved_at: int                  # its login or its last /api/auth/reauth (0: none)


_SESSION = ("SELECT id, token_hash, passkey_id, credential_id, user_agent, created_at, "
            "refreshed_at, expires_at, proved_at FROM sessions")


class AuthStore:
    def __init__(self, path: Path, clock=_now):
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(path, 0o600)
        self._lock = threading.Lock()
        # ponytail: in memory, a restart forgets it; a table if that is ever abused
        self._pin_fails: list[int] = []
        self._pin_until = 0
        migrate(path, MIGRATIONS)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self._db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # ---- the owner and passkeys -------------------------------------------------------
    def user_handle(self) -> bytes:
        return self._q("SELECT user_handle FROM owner")[0][0]

    def has_credentials(self) -> bool:
        """Any way to log in: a passkey, a password or a PIN. False is the first start."""
        return bool(self.methods())

    def methods(self) -> list[str]:
        """The kinds that can log in, in the login page's order."""
        kinds = {k for k, in self._q("SELECT 'passkey' FROM passkeys "
                                     "UNION SELECT kind FROM credentials")}
        return [k for k in ("passkey", "password", "pin") if k in kinds]

    def passkeys(self) -> list[dict]:
        rows = self._q("SELECT id, created_at, last_used_at FROM passkeys "
                       "ORDER BY created_at, rowid")
        return [{"id": cid, "created_at": c, "last_used_at": u} for cid, c, u in rows]

    def credential(self, cid: bytes) -> tuple[bytes, int] | None:
        rows = self._q("SELECT public_key, sign_count FROM passkeys WHERE id = ?", (cid,))
        return (rows[0][0], rows[0][1]) if rows else None

    def add_passkey(self, cid: bytes, public_key: bytes, sign_count: int) -> None:
        now = self.clock()
        with self._lock:
            self._db.execute(
                "INSERT INTO passkeys (id, public_key, sign_count, created_at, "
                "last_used_at) VALUES (?, ?, ?, ?, ?)",
                (cid, public_key, sign_count, now, now))
            self._db.execute("DELETE FROM setup_code")

    def used_passkey(self, cid: bytes, sign_count: int) -> None:
        self._x("UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE id = ?",
                (sign_count, self.clock(), cid))

    def remove_passkey(self, cid: bytes) -> bool:
        """Its sessions end with it (ON DELETE CASCADE)."""
        return self._x("DELETE FROM passkeys WHERE id = ?", (cid,)) > 0

    # ---- password and PIN -------------------------------------------------------------
    def credentials(self) -> list[dict]:
        rows = self._q("SELECT id, kind, label, created_at FROM credentials ORDER BY created_at, id")
        return [{"id": i, "kind": k, "label": l, "created_at": c} for i, k, l, c in rows]

    def credential_hashes(self, kind: str) -> list[tuple[int, bytes]]:
        return self._q("SELECT id, hash FROM credentials WHERE kind = ?", (kind,))

    def add_credential(self, kind: str, secret: str, label: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO credentials (kind, hash, label, created_at) VALUES (?, ?, ?, ?)",
                (kind, cred.hash(secret), label, self.clock()))
            self._db.execute("DELETE FROM setup_code")
            return cur.lastrowid

    def remove_credential(self, rid: int) -> bool:
        """Its sessions end with it (ON DELETE CASCADE)."""
        return self._x("DELETE FROM credentials WHERE id = ?", (rid,)) > 0

    # ---- sessions ---------------------------------------------------------------------
    def new_session(self, passkey: bytes | None = None, credential: int | None = None,
                    user_agent: str = "") -> str:
        """A login: a new session, proved now."""
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self._x("INSERT INTO sessions (token_hash, passkey_id, credential_id, user_agent, "
                "created_at, refreshed_at, expires_at, proved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (token_hash(token), passkey, credential, user_agent[:USER_AGENT_MAX], now, now,
                 now + SESSION_IDLE, now))
        return token

    def session(self, token: str | None) -> Session | None:
        """The live session for this cookie value, or None."""
        if not token:
            return None
        rows = self._q(_SESSION + " WHERE token_hash = ?", (token_hash(token),))
        if not rows:
            return None
        s = Session(*rows[0])
        if s.expires_at <= self.clock():
            self._x("DELETE FROM sessions WHERE token_hash = ?", (s.token_hash,))
            return None
        return s

    def needs_refresh(self, s: Session) -> bool:
        return self.clock() - s.refreshed_at >= SESSION_REFRESH

    def refresh(self, s: Session) -> int:
        """Slides the expiry forward; returns the new expiry."""
        now = self.clock()
        expires = min(now + SESSION_IDLE, s.created_at + SESSION_MAX)
        self._x("UPDATE sessions SET refreshed_at = ?, expires_at = ? WHERE token_hash = ?",
                (now, expires, s.token_hash))
        return expires

    def end_session(self, s: Session) -> None:
        self._x("DELETE FROM sessions WHERE token_hash = ?", (s.token_hash,))

    def sessions(self) -> list[Session]:
        """Every live session, the oldest first."""
        return [Session(*r) for r in self._q(_SESSION + " WHERE expires_at > ? ORDER BY id",
                                             (self.clock(),))]

    def end_session_id(self, sid: int) -> bool:
        return self._x("DELETE FROM sessions WHERE id = ?", (sid,)) > 0

    def end_other_sessions(self, s: Session) -> int:
        """Every session but this one ends; how many did."""
        return self._x("DELETE FROM sessions WHERE id != ?", (s.id,))

    def proved(self, s: Session) -> None:
        """The owner proved it again in this session (auth/routes.py, reauth)."""
        self._x("UPDATE sessions SET proved_at = ? WHERE id = ?", (self.clock(), s.id))

    def proven(self, s: Session) -> bool:
        """Proved within PROOF_SECONDS."""
        return self.clock() - s.proved_at <= PROOF_SECONDS

    def prune_sessions(self) -> None:
        self._x("DELETE FROM sessions WHERE expires_at <= ?", (self.clock(),))

    # ---- the setup code ---------------------------------------------------------------
    def new_setup_code(self) -> str:
        """A fresh one-time code; the one before stops working."""
        code = cred.new_code()
        self._x("INSERT OR REPLACE INTO setup_code (id, hash, created_at) VALUES (1, ?, ?)",
                (cred.hash(cred.normalize(code)), self.clock()))
        return code

    def claim_setup_code(self, stored: bytes) -> bool:
        """Uses the setup code up; False when it is gone or was replaced meanwhile."""
        return self._x("DELETE FROM setup_code WHERE hash = ?", (stored,)) == 1

    def setup_code(self) -> bytes | None:
        rows = self._q("SELECT hash FROM setup_code")
        return rows[0][0] if rows else None

    # ---- recovery codes ---------------------------------------------------------------
    def new_recovery_codes(self) -> list[str]:
        """Ten fresh codes; every earlier one stops working."""
        plain = [cred.new_code() for _ in range(cred.RECOVERY_COUNT)]
        hashed = [(cred.hash(cred.normalize(c)),) for c in plain]
        with self._lock, self._db:
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM recovery_codes")
            self._db.executemany("INSERT INTO recovery_codes (hash) VALUES (?)", hashed)
        return plain

    def unused_recovery_codes(self) -> list[tuple[int, bytes]]:
        return self._q("SELECT id, hash FROM recovery_codes WHERE used_at IS NULL")

    def use_recovery_code(self, rid: int) -> bool:
        return self._x("UPDATE recovery_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                       (self.clock(), rid)) == 1

    # ---- the two locks (module docstring) ---------------------------------------------
    def attempt(self, kind: str, addr: str = "") -> tuple[int, int]:
        """Claims one try of this kind before the secret is checked, so tries sent at
        the same time cannot slip past the lock. The try counts as failed until
        succeeded() clears the count. Returns (locked_until, failures): failures 0
        refuses the try (locked until then); with failures > 0 the try goes ahead and
        locked_until is the lock it starts if the secret is wrong (0: none). `addr`: the
        client address for "secret", "" for "code" (one lock for all)."""
        now = self.clock()
        with self._lock:
            row = self._db.execute("SELECT failures, locked_until FROM locks WHERE kind = ? AND addr = ?",
                                   (kind, addr)).fetchone()
            failures, until = row if row else (0, 0)
            if until > now:
                return until, 0
            failures += 1
            until = 0
            if failures >= FREE_TRIES:
                until = now + min(FIRST_LOCK * 2 ** min(failures - FREE_TRIES, 20), MAX_LOCK)
            self._db.execute("INSERT OR REPLACE INTO locks (kind, addr, failures, locked_until) "
                             "VALUES (?, ?, ?, ?)", (kind, addr, failures, until))
        return until, failures

    def pin_paused(self) -> int:
        """Until when PIN login is paused for every address (0: it is not). A PIN is short,
        and per-address locks do not stop one who rotates addresses."""
        with self._lock:
            return self._pin_until if self._pin_until > self.clock() else 0

    def pin_failed(self) -> None:
        """Counts a wrong PIN from anywhere; PIN_FAILS within PIN_WINDOW pause PIN login."""
        now = self.clock()
        with self._lock:
            self._pin_fails = [t for t in self._pin_fails if now - t < PIN_WINDOW] + [now]
            if len(self._pin_fails) >= PIN_FAILS:
                self._pin_until, self._pin_fails = now + PIN_PAUSE, []

    def succeeded(self, kind: str, addr: str = "") -> None:
        self._x("DELETE FROM locks WHERE kind = ? AND addr = ?", (kind, addr))
