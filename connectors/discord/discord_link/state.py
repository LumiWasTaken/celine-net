"""The connector's own state: one SQLite file in the state directory.

- journal: every canonical record the connector ever produced, `seq` rising, never
  pruned. The feed serves it; any number of app instances read it from any seq.
- seen: the last record per (kind, chat, message), so a message read twice is
  journaled once and a changed text becomes an edit record.
- channels: every DM, group DM and server channel the connector knows, with the DM
  sync tick (`enabled`; new DMs start unticked).
- ignored chats: unticked DMs and group DMs, and server channels ignored from the app
  (kv `ignored`). Nothing of theirs is journaled, and the feed skips what the journal
  already has of them.
- cursors: incremental REST sync per chat.
- spans: per chat, the message id ranges (lo to hi, both included) the journal holds
  all of: Discord's ids rise with time, so a read answers a covered range from the
  journal and asks Discord only for the rest (spans.py). lo 0 is the chat's start.
- subscriptions: the servers that are read along, with their settings (policy.py).
- kv: account, server list, blocked users, sync times.

The token is a separate file, `token`, mode 600, read by rest/gateway only.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS journal (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    chat_key    TEXT,
    message_key TEXT,
    ts          INTEGER,
    body        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    kind        TEXT NOT NULL,
    chat_key    TEXT NOT NULL,
    message_key TEXT NOT NULL,
    hash        TEXT NOT NULL,
    text        TEXT,
    seq         INTEGER NOT NULL,
    PRIMARY KEY (kind, chat_key, message_key)
);
CREATE TABLE IF NOT EXISTS channels (
    key             TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,           -- dm | group | channel
    type            INTEGER,                 -- Discord's channel type
    name            TEXT,
    guild_id        TEXT,
    guild_name      TEXT,
    parent_id       TEXT,                    -- category, or a thread's channel
    icon            TEXT,
    peer            TEXT,                    -- JSON user of a 1:1 DM
    last_message_id TEXT,
    enabled         INTEGER NOT NULL DEFAULT 0   -- DMs: synced
);
CREATE TABLE IF NOT EXISTS cursors (
    chat_key   TEXT PRIMARY KEY,
    newest     TEXT,
    oldest     TEXT,
    backfilled INTEGER NOT NULL DEFAULT 0,
    synced_at  INTEGER
);
CREATE TABLE IF NOT EXISTS spans (
    chat_key TEXT NOT NULL,
    lo       INTEGER NOT NULL,
    hi       INTEGER NOT NULL,
    PRIMARY KEY (chat_key, lo)
);
CREATE TABLE IF NOT EXISTS subscriptions (
    guild_id      TEXT PRIMARY KEY,
    guild_name    TEXT,
    icon          TEXT,
    enabled       INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    started_at    INTEGER,
    updated_at    INTEGER NOT NULL,
    settings      TEXT NOT NULL DEFAULT '{}',
    last_event_at INTEGER,
    events        INTEGER NOT NULL DEFAULT 0
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def chat_of(r: dict[str, Any]) -> str | None:
    """The chat a journal record belongs to (None for a person)."""
    if r.get("kind") in ("message", "chat"):
        return str((r.get("chat") or {}).get("key"))
    if r.get("kind") == "person":
        return None
    return str(r["chat_key"]) if r.get("chat_key") is not None else None


class Locked(RuntimeError):
    """Another discord-link already runs on this state directory."""


class State:
    def __init__(self, root: Path, *, lock: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._lockf = None
        if lock:
            self._lockf = open(self.root / "lock", "w")
            try:
                fcntl.flock(self._lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._lockf.close()
                raise Locked(f"another discord-link uses {self.root}") from None
        self.media = self.root / "media"
        self.media.mkdir(exist_ok=True)
        self._mu = threading.RLock()
        self.conn = sqlite3.connect(self.root / "state.db", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._head = self._read_head()

    def close(self) -> None:
        with self._mu:
            self.conn.close()
        if self._lockf is not None:
            self._lockf.close()

    # ---- plumbing ----------------------------------------------------------------------
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._mu:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._mu:
            return self.conn.execute(sql, tuple(args)).fetchone()

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._mu:
            cur = self.conn.execute(sql, tuple(args))
            self.conn.commit()
            return cur.rowcount

    # ---- kv ----------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
               (key, json.dumps(value)))

    # ---- the token ---------------------------------------------------------------------
    @property
    def token_path(self) -> Path:
        return self.root / "token"

    def token(self) -> str | None:
        try:
            t = self.token_path.read_text().strip()
        except FileNotFoundError:
            return None
        return t or None

    def save_token(self, token: str) -> None:
        tmp = self.root / "token.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.replace(tmp, self.token_path)
        self.set("token_rejected", None)

    def forget_token(self) -> bool:
        if self.token_path.exists():
            self.token_path.unlink()
            return True
        return False

    # ---- the journal -------------------------------------------------------------------
    def _read_head(self) -> int:
        row = self.one("SELECT COALESCE(MAX(seq), 0) FROM journal")
        return int(row[0])

    @property
    def head(self) -> int:
        return self._head

    def journal(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Append records that say something new. Returns every record with its seq:
        a record identical to the last one for its (kind, chat, message) keeps that
        one's seq. A message whose text changed since it was journaled also gets an
        edit record, so the archive keeps the old text. Records of an ignored chat are
        dropped."""
        out: list[dict[str, Any]] = []
        with self._mu:
            ignored = self.ignored()
            try:
                for r in records:
                    if chat_of(r) in ignored:
                        continue
                    out.extend(self._append(r))
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                self._head = self._read_head()
                raise
        return out

    def _key(self, r: dict[str, Any]) -> tuple[str, str, str]:
        kind = r["kind"]
        if kind == "message":
            return kind, str(r["chat"]["key"]), str(r["message"]["key"])
        if kind == "chat":
            return kind, str(r["chat"]["key"]), ""
        if kind == "person":
            return kind, str(r["person"]["key"]), ""
        if kind == "delete":
            return kind, str(r["chat_key"]), ",".join(r["message_keys"])
        return kind, str(r.get("chat_key") or ""), str(r.get("message_key") or "")

    def _append(self, r: dict[str, Any]) -> list[dict[str, Any]]:
        r = {k: v for k, v in r.items() if k != "seq"}
        body = json.dumps(r, ensure_ascii=False, sort_keys=True)
        h = hashlib.sha1(body.encode()).hexdigest()
        kind, chat_key, message_key = self._key(r)
        prev = self.conn.execute("SELECT hash, text, seq FROM seen WHERE kind = ? AND "
                                 "chat_key = ? AND message_key = ?",
                                 (kind, chat_key, message_key)).fetchone()
        if prev is not None and prev["hash"] == h:
            return [{"seq": prev["seq"], **r}]
        out = []
        text = r["message"].get("text") if kind == "message" else None
        if kind == "message" and prev is not None and text is not None \
                and prev["text"] is not None and prev["text"] != text:
            out.append(self._insert({"kind": "edit", "chat_key": chat_key,
                                     "message_key": message_key, "ts": now_ms(),
                                     "text": text}))
        rec = self._insert(r, body)
        self.conn.execute(
            "INSERT INTO seen (kind, chat_key, message_key, hash, text, seq) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (kind, chat_key, message_key) DO UPDATE "
            "SET hash = excluded.hash, text = excluded.text, seq = excluded.seq",
            (kind, chat_key, message_key, h, text, rec["seq"]))
        out.append(rec)
        return out

    def _insert(self, r: dict[str, Any], body: str | None = None) -> dict[str, Any]:
        kind, chat_key, message_key = self._key(r)
        ts = (r.get("message") or {}).get("ts") or r.get("ts")
        body = body or json.dumps(r, ensure_ascii=False, sort_keys=True)
        cur = self.conn.execute(
            "INSERT INTO journal (kind, chat_key, message_key, ts, body) "
            "VALUES (?, ?, ?, ?, ?)", (kind, chat_key, message_key or None, ts, body))
        self._head = cur.lastrowid
        return {"seq": cur.lastrowid, **r}

    def after(self, seq: int, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.q("SELECT seq, body FROM journal WHERE seq > ? ORDER BY seq LIMIT ?",
                      (seq, limit))
        return [{"seq": r["seq"], **json.loads(r["body"])} for r in rows]

    # ---- ignored chats -----------------------------------------------------------------
    def ignored(self) -> set[str]:
        keys = {r[0] for r in self.q("SELECT key FROM channels WHERE kind IN ('dm', 'group') "
                                     "AND enabled = 0")}
        return keys | set(self.get("ignored", []) or [])

    def is_ignored(self, key: str) -> bool:
        return str(key) in self.ignored()

    def ignore(self, key: str) -> None:
        """A server channel (or thread) left out. DMs are left out by their tick."""
        with self._mu:
            keys = set(self.get("ignored", []) or [])
            if str(key) not in keys:
                self.set("ignored", sorted(keys | {str(key)}))

    def unignore(self, keys: Iterable[str]) -> None:
        with self._mu:
            now = set(self.get("ignored", []) or [])
            left = now - {str(k) for k in keys}
            if left != now:
                self.set("ignored", sorted(left))

    def forget(self, key: str) -> None:
        """Drop the chat's cursor, its spans and what was seen of it, so including it
        again reads and journals it from the start (the app deleted its messages)."""
        with self._mu:
            self.conn.execute("DELETE FROM cursors WHERE chat_key = ?", (str(key),))
            self.conn.execute("DELETE FROM spans WHERE chat_key = ?", (str(key),))
            self.conn.execute("DELETE FROM seen WHERE chat_key = ? AND kind <> 'person'",
                              (str(key),))
            self.conn.commit()

    # ---- spans: the id ranges the journal holds all of ---------------------------------
    def spans(self, chat: str) -> list[tuple[int, int]]:
        return [(r[0], r[1]) for r in self.q(
            "SELECT lo, hi FROM spans WHERE chat_key = ? ORDER BY lo", (str(chat),))]

    def span_of(self, chat: str, mid: int) -> tuple[int, int] | None:
        """The span that holds the id, if any."""
        r = self.one("SELECT lo, hi FROM spans WHERE chat_key = ? AND lo <= ? AND hi >= ?",
                     (str(chat), mid, mid))
        return (r[0], r[1]) if r else None

    def cover(self, chat: str, lo: int, hi: int) -> None:
        """Every message of the chat from lo to hi is journaled now. Spans that overlap
        or touch it become one. A chat left out keeps none: nothing of it is journaled."""
        if lo > hi:
            return
        with self._mu:
            if self.is_ignored(chat):
                return
            try:
                rows = self.conn.execute(
                    "SELECT lo, hi FROM spans WHERE chat_key = ? AND lo <= ? AND hi >= ?",
                    (str(chat), hi + 1, lo - 1)).fetchall()
                lo = min([lo, *(r[0] for r in rows)])
                hi = max([hi, *(r[1] for r in rows)])
                self.conn.execute("DELETE FROM spans WHERE chat_key = ? AND lo >= ? AND "
                                  "hi <= ?", (str(chat), lo, hi))
                self.conn.execute("INSERT INTO spans (chat_key, lo, hi) VALUES (?, ?, ?)",
                                  (str(chat), lo, hi))
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def top(self, chat: str) -> int:
        """The newest message id the connector knows of the chat (0: none)."""
        r = self.one("SELECT MAX(CAST(message_key AS INTEGER)) FROM seen WHERE kind = "
                     "'message' AND chat_key = ?", (str(chat),))
        ch = self.one("SELECT last_message_id FROM channels WHERE key = ?", (str(chat),))
        return max(int(r[0] or 0), int(ch[0] or 0) if ch else 0)

    def archived(self, chat: str, lo: int, hi: int, limit: int, *,
                 newest: bool) -> list[dict[str, Any]]:
        """The chat's journaled messages from lo to hi as records with their seq, at
        most `limit` from the newest or the oldest end, oldest first. A message deleted
        since is left out, and one edited since has its latest text, as Discord would
        answer now."""
        rows = self.q(
            f"""SELECT s.text, j.seq, j.body FROM seen s JOIN journal j ON j.seq = s.seq
                WHERE s.kind = 'message' AND s.chat_key = ?
                  AND CAST(s.message_key AS INTEGER) BETWEEN ? AND ?
                  AND s.message_key NOT IN (
                      SELECT k.value FROM journal d, json_each(d.body, '$.message_keys') k
                      WHERE d.kind = 'delete' AND d.chat_key = ?)
                ORDER BY CAST(s.message_key AS INTEGER) {'DESC' if newest else 'ASC'}
                LIMIT ?""", (str(chat), lo, hi, str(chat), limit))
        out = []
        for r in rows:
            rec = {"seq": r["seq"], **json.loads(r["body"])}
            m = rec["message"]
            if r["text"] is not None and r["text"] != m.get("text"):
                m["text"] = r["text"]
                m["edited"] = True
                if (m.get("media") or {}).get("caption") is not None:
                    m["media"]["caption"] = r["text"]
            out.append(rec)
        return out[::-1] if newest else out

    # ---- channels ----------------------------------------------------------------------
    def channel(self, key: str) -> dict[str, Any] | None:
        r = self.one("SELECT * FROM channels WHERE key = ?", (str(key),))
        if r is None:
            return None
        d = dict(r)
        d["peer"] = json.loads(d["peer"]) if d["peer"] else None
        return d

    def learn_channel(self, key: str, *, kind: str, type: int | None = None,
                      name: str | None = None, guild_id: str | None = None,
                      guild_name: str | None = None, parent_id: str | None = None,
                      icon: str | None = None, peer: dict | None = None,
                      last_message_id: str | None = None, enabled: int | None = None) -> None:
        self.x(
            """INSERT INTO channels (key, kind, type, name, guild_id, guild_name, parent_id,
                                     icon, peer, last_message_id, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 0))
               ON CONFLICT (key) DO UPDATE SET kind = excluded.kind,
                 type = COALESCE(excluded.type, channels.type),
                 name = COALESCE(excluded.name, channels.name),
                 guild_id = COALESCE(excluded.guild_id, channels.guild_id),
                 guild_name = COALESCE(excluded.guild_name, channels.guild_name),
                 parent_id = COALESCE(excluded.parent_id, channels.parent_id),
                 icon = COALESCE(excluded.icon, channels.icon),
                 peer = COALESCE(excluded.peer, channels.peer),
                 last_message_id = CASE WHEN excluded.last_message_id IS NOT NULL AND
                     (channels.last_message_id IS NULL OR
                      CAST(excluded.last_message_id AS INTEGER) >
                      CAST(channels.last_message_id AS INTEGER))
                   THEN excluded.last_message_id ELSE channels.last_message_id END,
                 enabled = COALESCE(?, channels.enabled)""",
            (str(key), kind, type, name, guild_id, guild_name, parent_id, icon,
             json.dumps(peer) if peer else None, last_message_id, enabled, enabled))
